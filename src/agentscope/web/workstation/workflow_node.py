# -*- coding: utf-8 -*-
"""Workflow node opt."""
import base64
import copy
import json
import traceback
from abc import ABC, abstractmethod
from enum import IntEnum
from typing import List, Optional
from loguru import logger
from typing import Any
from concurrent.futures import ThreadPoolExecutor

import agentscope
from agentscope import msghub
from agentscope.agents import (
    DialogAgent,
    UserAgent,
    TextToImageAgent,
    DictDialogAgent,
    ReActAgent,
)
from agentscope.message import Msg
from agentscope.models import read_model_configs
from agentscope.pipelines import (
    SequentialPipeline,
    ForLoopPipeline,
    WhileLoopPipeline,
    IfElsePipeline,
    SwitchPipeline,
)
from agentscope.pipelines.functional import placeholder
from agentscope.web.workstation.workflow_utils import (
    kwarg_converter,
    deps_converter,
    dict_converter,
)
from agentscope.service import (
    bing_search,
    google_search,
    read_text_file,
    write_text_file,
    execute_python_code,
    ServiceFactory,
    api_request,
    service_status,
)

from agentscope.service.web.apiservice import api_request_for_big_model
from agentscope.web.workstation.workflow_utils import WorkflowNodeStatus
from agentscope.aibigmodel_workflow.config import LLM_URL, RAG_URL, LLM_TOKEN

try:
    import networkx as nx
except ImportError:
    nx = None

DEFAULT_FLOW_VAR = "flow"


def remove_duplicates_from_end(lst: list) -> list:
    """remove duplicates element from end on a list"""
    seen = set()
    result = []
    for item in reversed(lst):
        if item not in seen:
            seen.add(item)
            result.append(item)
    result.reverse()
    return result


def parse_json_to_dict(extract_text: str) -> dict:
    # Parse the content into JSON object
    try:
        parsed_json = json.loads(extract_text, strict=False)
        if not isinstance(parsed_json, dict):
            raise Exception(f"json text type({type(parsed_json)}) is not dict")
        return parsed_json
    except json.decoder.JSONDecodeError as e:
        raise e


class ASDiGraph(nx.DiGraph):
    """
    A class that represents a directed graph, extending the functionality of
    networkx's DiGraph to suit specific workflow requirements in AgentScope.

    This graph supports operations such as adding nodes with associated
    computations and executing these computations in a topological order.

    Attributes:
        nodes_not_in_graph (set): A set of nodes that are not included in
        the computation graph.
    """

    def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        """
        Initialize the ASDiGraph instance.
        """
        super().__init__(*args, **kwargs)
        self.nodes_not_in_graph = set()

        # Prepare the header of the file with necessary imports and any
        # global definitions
        self.imports = [
            "import agentscope",
        ]

        self.inits = [
            'agentscope.init(logger_level="DEBUG")',
            f"{DEFAULT_FLOW_VAR} = None",
        ]

        self.execs = ["\n"]
        self.config = {}
        kwargs.setdefault('uuid', None)
        self.uuid = kwargs['uuid']
        self.params_pool = {}
        self.conditions_pool = {}
        self.selected_branch = -1

    def generate_node_param_real(self, param_spec: dict) -> dict:
        param_name = param_spec['name']

        if self.params_pool and param_spec['value']['type'] == 'ref':
            reference_node_name = param_spec['value']['content']['ref_node_id']
            reference_param_name = param_spec['value']['content']['ref_var_name']
            if self.nodes[reference_node_name]["opt"].running_status == WorkflowNodeStatus.RUNNING_SKIP:
                return {param_name: None}
            param_value = self.params_pool[reference_node_name][reference_param_name]
            return {param_name: param_value}

        return {param_name: param_spec['value']['content']}

    def generate_node_param_real_for_switch_input(self, param_spec: dict) -> dict:
        for condition in param_spec['conditions']:
            if not condition['left'] and not condition['right']:
                raise Exception("Switch node format invalid")
            if condition['left']['value']['type'] == 'ref':
                reference_node_name = condition['left']['value']['content']['ref_node_id']
                reference_param_name = condition['left']['value']['content']['ref_var_name']
                param_value = self.params_pool[reference_node_name][reference_param_name]
                condition['left']['value']['content'] = param_value
            elif condition['right']['value']['type'] == 'ref':
                reference_node_name = condition['right']['value']['content']['ref_node_id']
                reference_param_name = condition['right']['value']['content']['ref_var_name']
                param_value = self.params_pool[reference_node_name][reference_param_name]
                condition['right']['value']['content'] = param_value

        return param_spec

    def generate_and_execute_condition_python_code(self, switch_node_id, condition_data, branch):
        logic = condition_data['logic']
        target_node_id = condition_data['target_node_id']
        if not target_node_id:
            raise Exception("分支器节点存在未连接的分支")
        conditions = condition_data['conditions']
        condition_str = ""

        # conditions为空列表且之前的条件不满足时，代表条件为else
        if not conditions:
            if (switch_node_id not in self.conditions_pool or
                    all(value is False for value in self.conditions_pool[switch_node_id].values())):
                self.update_conditions_pool_with_running_node(switch_node_id, target_node_id, True)
            else:
                self.update_conditions_pool_with_running_node(switch_node_id, target_node_id, False)
        else:
            for i, condition in enumerate(conditions):
                left = condition['left']
                right = condition['right']
                operator = condition['operator']
                # 右边参数类型支持ref和literal
                right_data_type = right['value']['type']

                left_value = left['value']['content']
                right_value = right['value']['content']

                condition_str += self.generate_operator_comparison(operator, left_value, right_value, right_data_type)

                if i < len(conditions) - 1:
                    condition_str += f" {logic} "
            logger.info(f"======= Switch Node condition_str {condition_str}")
            try:
                # 使用 eval 函数进行求值
                condition_func = eval(condition_str)
            except Exception as e:
                error_message = f"条件语句错误: {e}"
                raise Exception(error_message)

            # 确认当前条件成功的分支
            if self.selected_branch == -1:
                self.selected_branch = branch + 1 if condition_func else -1

            # 添加执行结果到conditions_pool中，后续节点进行判断
            self.update_conditions_pool_with_running_node(switch_node_id, target_node_id, condition_func)

    def generate_operator_comparison(self, operator, left_value, right_value, data_type) -> str:
        if data_type == "ref":
            if isinstance(left_value, str):
                left_value = f"{repr(left_value)}"
            if isinstance(right_value, str):
                right_value = f"{repr(right_value)}"
            switcher = {
                'eq': f"{left_value} == {right_value}",
                'not_eq': f"{left_value} != {right_value}",
                'len_ge': f"len({left_value}) >= len({right_value})",
                'len_gt': f"len({left_value}) > len({right_value})",
                'len_le': f"len({left_value}) <= len({right_value})",
                'len_lt': f"len({left_value}) < len({right_value})",
                'empty': f"{left_value} is None",
                'not_empty': f"{left_value} is not None",
                'in': f"{right_value} in {left_value}",
                'not_in': f"{right_value} not in {left_value}"
            }
        else:
            if isinstance(left_value, str):
                left_value = f"{repr(left_value)}"
            switcher = {
                'eq': f"{left_value} == '{right_value}'",
                'not_eq': f"{left_value} != '{right_value}'",
                'len_ge': f"len({left_value}) >= {right_value}",
                'len_gt': f"len({left_value}) > {right_value}",
                'len_le': f"len({left_value}) <= {right_value}",
                'len_lt': f"len({left_value}) < {right_value}",
                'empty': f"{left_value} is None",
                'not_empty': f"{left_value} is not None",
                'in': f"'{right_value}' in {left_value}",
                'not_in': f"'{right_value}' not in {left_value}"
            }
        try:
            return switcher.get(operator)
        except KeyError:
            raise ValueError(f"Unsupported operator: {operator}")

    def confirm_current_node_running_status(self, node_id, input_params) -> (str, bool):
        predecessors_list = list(self.predecessors(node_id))
        # 0. 适配单节点, 节点无前驱节点且输入参数为空时，节点为等待状态
        if len(input_params) == 0 and not predecessors_list:
            return WorkflowNodeStatus.INIT, False
        # 1. 节点前驱节点存在失败节点, 节点为等待状态
        if any(
                self.nodes[node]["opt"].running_status == WorkflowNodeStatus.FAILED for node
                in predecessors_list):
            return WorkflowNodeStatus.INIT, False
        # 2. 节点前驱节点存在Switch节点, 且节点所在分支满足条件或不存在在Switch分支后， 优先级1 (前序节点存在未运行节点不影响当前节点状态)
        if self.validate_node_condition(node_id, predecessors_list):
            return WorkflowNodeStatus.RUNNING, True
        # 3. 节点前驱节点存在Switch节点, 且节点所在分支不满足条件, 置为未运行状态，节点返回空值
        # 3.1 节点前驱节点不存在Switch节点, 且节点前序节点存在未运行状态时，该节点状态为未运行
        if not self.validate_predecessor_condition(node_id, predecessors_list):
            return WorkflowNodeStatus.RUNNING_SKIP, False
        # 3.2 节点前驱节点不存在Switch节点, 且节点前序节点均为运行时，该节点为运行
        else:
            return WorkflowNodeStatus.RUNNING, True

    def validate_node_condition(self, node_id, predecessors_list) -> bool:
        logger.info(f"Switch Node判断池 {self.conditions_pool}")
        if not self.conditions_pool:
            return True

        switch_node = next(
            (item for item in predecessors_list if self.nodes[item]["opt"].node_type == WorkflowNodeType.SWITCH), None)

        if switch_node not in self.conditions_pool:
            return False

        if switch_node and self.conditions_pool[switch_node][node_id]:
            return True
        else:
            return False

    def validate_predecessor_condition(self, node_id, predecessors_list) -> bool:
        predecessor_status = {}
        predecessor_node_type = {}
        for item in predecessors_list:
            predecessor_status[item] = self.nodes[item]["opt"].running_status
            predecessor_node_type[item] = self.nodes[item]["opt"].node_type

        logger.info(
            f"节点 {node_id}, predecessor_status: {predecessor_status}, "
            f"predecessor_node_type: {predecessor_node_type}")
        # 如果全部前驱节点处于 'running_skip' 状态，则不执行当前节点
        if all(s == WorkflowNodeStatus.RUNNING_SKIP for s in predecessor_status.values()):
            return False

        # 检查前驱节点是否有运行成功的节点，如果前驱节点全部为成功节点，则当前节点为”running“并且排除分支器节点
        if (all(s in {WorkflowNodeStatus.SUCCESS, WorkflowNodeStatus.RUNNING_SKIP}
                for s in predecessor_status.values()) and not
        any(t in {WorkflowNodeType.SWITCH} for t in predecessor_node_type.values())):
            return True

        return False

    def generate_node_param_real_for_api_input(self, param_spec: dict) -> (dict, dict):
        query_param_result, json_body_param_result = {}, {}

        param_name = param_spec['name']
        param_spec.setdefault('extra', {})
        param_spec['extra'].setdefault('location', '')
        param_type_location = param_spec['extra']['location']

        if param_type_location == 'query':
            param_result = self.generate_node_param_real(param_spec)
            query_param_result = param_result
        elif param_type_location == 'body':
            param_result = self.generate_node_param_real(param_spec)
            json_body_param_result = param_result
        else:
            raise Exception("param: {param_spec} location type: {param_type_location} invalid")

        return query_param_result, json_body_param_result

    def init_params_pool_with_running_node(self, node_id: str):
        # 注意到多个节点并发运行时，字典params_pool是共享变量，
        # 但是由于GIL的机制天然保证了多线程之间的串行运行顺序，以及每个节点只操作字典params_pool的各自不同key，这里简单起见，可以不加锁
        # TODO 加 threading lock
        self.params_pool.setdefault(node_id, {})
        return

    def update_params_pool_with_running_node(self, node_id: str, node_output_params: dict):
        # 注意到多个节点并发运行时，字典params_pool是共享变量，
        # 但是由于GIL的机制天然保证了多线程之间的串行运行顺序，以及每个节点只操作字典params_pool的各自不同key，这里简单起见，可以不加锁
        # TODO 加 threading lock
        self.params_pool[node_id] |= node_output_params

    def update_conditions_pool_with_running_node(self, switch_node_id, target_node_id: str, condition_result: bool):
        # 注意到多个节点并发运行时，字典params_pool是共享变量，
        # 但是由于GIL的机制天然保证了多线程之间的串行运行顺序，以及每个节点只操作字典params_pool的各自不同key，这里简单起见，可以不加锁
        # TODO 加 threading lock
        self.conditions_pool.setdefault(switch_node_id, {})

        # 当switch节点有除当前分支之外分支连到了同一节点，且另一分支执行结果为True时，不覆盖条件判断池中的值
        if (target_node_id in self.conditions_pool[switch_node_id] and
                self.conditions_pool[switch_node_id][target_node_id] is True):
            self.conditions_pool[switch_node_id][target_node_id] = True
            return

        # 条件从第一个到最后一个分支优先级排列，前面的条件优先级更高
        if not any(value is True for value in self.conditions_pool[switch_node_id].values()):
            self.conditions_pool[switch_node_id][target_node_id] = condition_result
        else:
            self.conditions_pool[switch_node_id][target_node_id] = False

    @staticmethod
    def generate_node_param_spec(param_spec: dict) -> dict:
        param_name = param_spec['name']
        return {param_name: param_spec['value']['content']}

    @staticmethod
    def set_initial_nodes_result(config: dict) -> list:
        nodes = config.get("nodes", [])
        node_result = []
        for node in nodes:
            node_result_dict = {
                "node_id": node["id"],
                "node_status": "",
                "node_message": "",
                "node_type": node["type"],
                "inputs": {},
                "outputs": {},
                "node_execute_cost": ""
            }
            node_result.append(node_result_dict)
        return node_result

    @staticmethod
    def update_nodes_with_values(nodes, output_values, input_values, status_values, message_values):
        for index, node in enumerate(nodes):
            node_id = node['node_id']
            node['node_status'] = status_values.get(node_id, WorkflowNodeStatus.INIT)
            node['node_message'] = message_values.get(node_id, "")
            node['inputs'] = input_values.get(node_id, {})
            node['outputs'] = output_values.get(node_id, {})
            nodes[index] = node

        return nodes

    def save(self, save_filepath: str = "", ) -> None:
        if len(self.config) > 0:
            # Write the script to file
            with open(save_filepath, "w", encoding="utf-8") as file:
                json.dump(self.config, file)

    def run(self) -> None:
        """
        Execute the computations associated with each node in the graph.

        The method initializes AgentScope, performs a topological sort of
        the nodes, and then runs each node's computation sequentially using
        the outputs from its predecessors as inputs.
        """
        agentscope.init(logger_level="DEBUG")
        sorted_nodes = list(nx.topological_sort(self))
        sorted_nodes = [
            node_id
            for node_id in sorted_nodes
            if node_id not in self.nodes_not_in_graph
        ]
        logger.info(f"sorted_nodes: {sorted_nodes}")
        logger.info(f"nodes_not_in_graph: {self.nodes_not_in_graph}")

        # Cache output
        values = {}

        # Run with predecessors outputs
        for node_id in sorted_nodes:
            inputs = [
                values[predecessor]
                for predecessor in self.predecessors(node_id)
            ]
            logger.info(f"inputs: {inputs}")
            print("len(inputs): ", len(inputs))
            if not inputs:
                values[node_id] = self.exec_node(node_id, None)
            elif len(inputs):
                # Note: only support exec with the first predecessor now
                values[node_id] = self.exec_node(node_id, inputs[0])
            else:
                raise ValueError("Too many predecessors!")

    def run_with_param(self, input_param: dict, config: dict) -> (Any, dict):
        """
        Execute the computations associated with each node in the graph.

        The method initializes AgentScope, performs a topological sort of
        the nodes, and then runs each node's computation sequentially using
        the outputs from its predecessors as inputs.
        """
        agentscope.init(logger_level="DEBUG")

        # 注释掉原来的代码，
        # 原因是，能并行的节点，尽量多线程并发运行，所以这里使用产出的二维数组来生成运行序列结果，参见 topological_generations文档
        #
        # sorted_nodes = list(nx.topological_sort(self))
        # sorted_nodes = [
        #     node_id
        #     for node_id in sorted_nodes
        #     if node_id not in self.nodes_not_in_graph
        # ]
        sorted_nodes_generations = list(nx.topological_generations(self))
        logger.info(f"topological generation sorted nodes: {sorted_nodes_generations}")

        total_input_values, total_output_values, total_status_values, total_message_values = {}, {}, {}, {}
        output_values = {}
        # Run with predecessors outputs
        for nodes_each_generation in sorted_nodes_generations:
            # 串行运行
            if len(nodes_each_generation) == 1:
                for node_id in nodes_each_generation:
                    inputs = [
                        copy.deepcopy(output_values[predecessor])
                        for predecessor in self.predecessors(node_id)
                    ]

                    # 运行节点，并保存输出参数
                    all_predecessor_node_output_params = {}
                    for i, predecessor_node_output_params in enumerate(inputs):
                        all_predecessor_node_output_params |= predecessor_node_output_params
                    # 特殊情况：注意开始节点
                    if len(all_predecessor_node_output_params) == 0:
                        all_predecessor_node_output_params = input_param
                    output_values[node_id] = self.exec_node(node_id, all_predecessor_node_output_params)
                continue

            # 并发运行
            if len(nodes_each_generation) > 1:
                node_and_inputparams = [[], []]
                for node_id in nodes_each_generation:
                    inputs = [
                        copy.deepcopy(output_values[predecessor])
                        for predecessor in self.predecessors(node_id)
                    ]

                    all_predecessor_node_output_params = {}
                    for i, predecessor_node_output_params in enumerate(inputs):
                        all_predecessor_node_output_params |= predecessor_node_output_params
                    # 注意到这里一定不是开始节点，所以省略 all_predecessor_node_output_params = input_param
                    node_and_inputparams[0].append(node_id)
                    node_and_inputparams[1].append(all_predecessor_node_output_params)

                # 运行节点，并保存输出参数
                with ThreadPoolExecutor() as executor:
                    res = executor.map(self.exec_node, node_and_inputparams[0], node_and_inputparams[1])
                    for index, result in enumerate(res):
                        node_id = node_and_inputparams[0][index]
                        output_values[node_id] = result
                continue

            # 异常代码区
            raise Exception("dag图拓扑排序失败！")

        end_node_id = -1
        for nodes_each_generation in sorted_nodes_generations:
            for node_id in nodes_each_generation:
                # 保存各个节点的信息
                total_input_values[node_id] = self.nodes[node_id]["opt"].input_params
                total_output_values[node_id] = self.nodes[node_id]["opt"].output_params
                total_status_values[node_id] = self.nodes[node_id]["opt"].running_status
                total_message_values[node_id] = self.nodes[node_id]["opt"].running_message
                end_node_id = node_id

        # 初始化节点运行结果并更新
        nodes_result = ASDiGraph.set_initial_nodes_result(config)
        updated_nodes_result = ASDiGraph.update_nodes_with_values(
            nodes_result, total_output_values, total_input_values, total_status_values, total_message_values)
        logger.info(f"workflow total running result: {updated_nodes_result}")
        return total_input_values[end_node_id], updated_nodes_result

    def compile(  # type: ignore[no-untyped-def]
            self,
            compiled_filename: str = "",
            **kwargs,
    ) -> str:
        """Compile DAG to a runnable python code"""

        def format_python_code(code: str) -> str:
            try:
                from black import FileMode, format_str

                logger.debug("Formatting Code with black...")
                return format_str(code, mode=FileMode())
            except Exception:
                return code

        self.inits[
            0
        ] = f'agentscope.init(logger_level="DEBUG", {kwarg_converter(kwargs)})'

        sorted_nodes = list(nx.topological_sort(self))
        sorted_nodes = [
            node_id
            for node_id in sorted_nodes
            if node_id not in self.nodes_not_in_graph
        ]

        for node_id in sorted_nodes:
            node = self.nodes[node_id]
            self.execs.append(node["compile_dict"]["execs"])

        header = "\n".join(self.imports)

        # Remove duplicate import
        new_imports = remove_duplicates_from_end(header.split("\n"))
        header = "\n".join(new_imports)
        body = "\n    ".join(self.inits + self.execs)

        main_body = f"def main():\n    {body}"

        # Combine header and body to form the full script
        script = (
            f"{header}\n\n\n{main_body}\n\nif __name__ == "
            f"'__main__':\n    main()\n"
        )

        formatted_code = format_python_code(script)

        logger.info(f"compiled_filename: {compiled_filename}")
        if len(compiled_filename) > 0:
            # Write the script to file
            with open(compiled_filename, "w", encoding="utf-8") as file:
                file.write(formatted_code)
        return formatted_code

    # pylint: disable=R0912
    def add_as_node(
            self,
            node_id: str,
            node_info: dict,
            config: dict,
    ) -> Any:
        """
        Add a node to the graph based on provided node information and
        configuration.

        Args:
            node_id (str): The identifier for the node being added.
            node_info (dict): A dictionary containing information about the
                node.
            config (dict): Configuration information for the node dependencies.

        Returns:
            The computation object associated with the added node.
        """
        node_cls = NODE_NAME_MAPPING[node_info.get("name", "")]
        # 适配新增节点
        if node_cls.node_type not in [
            WorkflowNodeType.MODEL,
            WorkflowNodeType.AGENT,
            WorkflowNodeType.MESSAGE,
            WorkflowNodeType.PIPELINE,
            WorkflowNodeType.COPY,
            WorkflowNodeType.SERVICE,
            WorkflowNodeType.START,
            WorkflowNodeType.END,
            WorkflowNodeType.PYTHON,
            WorkflowNodeType.API,
            WorkflowNodeType.LLM,
            WorkflowNodeType.SWITCH,
            WorkflowNodeType.RAG,
        ]:
            raise NotImplementedError(node_cls)

        if self.has_node(node_id):
            return self.nodes[node_id]["opt"]

        # Init dep nodes
        deps = [str(n) for n in node_info.get("data", {}).get("elements", [])]

        # Exclude for dag when in a Group
        if node_cls.node_type != WorkflowNodeType.COPY:
            self.nodes_not_in_graph = self.nodes_not_in_graph.union(set(deps))

        dep_opts = []
        for dep_node_id in deps:
            if not self.has_node(dep_node_id):
                dep_node_info = config[dep_node_id]
                self.add_as_node(dep_node_id, dep_node_info, config)
            dep_opts.append(self.nodes[dep_node_id]["opt"])

        node_opt = node_cls(
            node_id=node_id,
            opt_kwargs=node_info["data"].get("args", {}),
            source_kwargs=node_info["data"].get("source", {}),
            dep_opts=dep_opts,
            dag_obj=self,
        )

        # Add build compiled python code
        compile_dict = node_opt.compile()

        self.add_node(
            node_id,
            opt=node_opt,
            compile_dict=compile_dict,
            **node_info,
        )

        # Insert compile information to imports and inits
        self.imports.append(compile_dict["imports"])

        if node_cls.node_type == WorkflowNodeType.MODEL:
            self.inits.insert(1, compile_dict["inits"])
        else:
            self.inits.append(compile_dict["inits"])
        return node_opt

    def exec_node(self, node_id: str, x_in: Any) -> Any:
        """
        Execute the computation associated with a given node in the graph.

        Args:
            node_id (str): The identifier of the node whose computation is
                to be executed.
            x_in: The input to the node's computation. Defaults to None.

        Returns:
            The output of the node's computation.
        """
        opt = self.nodes[node_id]["opt"]
        # logger.info(f"{node_id}, {opt}, x_in: {x_in}")
        if not x_in and not isinstance(x_in, dict):
            raise Exception(f'x_in type:{type(x_in)} not dict')
        out_values = opt(**x_in)
        return out_values


class WorkflowNodeType(IntEnum):
    """Enum for workflow node.
    添加了两种类型，START和END类型"""

    MODEL = 0
    AGENT = 1
    PIPELINE = 2
    SERVICE = 3
    MESSAGE = 4
    COPY = 5
    START = 6
    END = 7
    PYTHON = 8
    API = 9
    LLM = 10
    SWITCH = 11
    RAG = 12


class WorkflowNode(ABC):
    """
    Abstract base class representing a generic node in a workflow.

    WorkflowNode is designed to be subclassed with specific logic implemented
    in the subclass methods. It provides an interface for initialization and
    execution of operations when the node is called.
    """

    node_type = None

    def __init__(
            self,
            node_id: str,
            opt_kwargs: dict,
            source_kwargs: dict,
            dep_opts: list,
    ) -> None:
        """
        Initialize nodes. Implement specific initialization logic in
        subclasses.
        """
        self.node_id = node_id
        self.opt_kwargs = opt_kwargs
        self.source_kwargs = source_kwargs
        self.dep_opts = dep_opts
        self.dep_vars = [opt.var_name for opt in self.dep_opts]
        self.var_name = f"{self.node_type.name.lower()}_{self.node_id}"

    def __call__(self, x: dict = None):  # type: ignore[no-untyped-def]
        """
        Performs the operations of the node. Implement specific logic in
        subclasses.
        """

    @abstractmethod
    def compile(self) -> dict:
        """
        Compile Node to python executable code dict
        """
        return {
            "imports": "",
            "inits": "",
            "execs": "",
        }


class ModelNode(WorkflowNode):
    """
    A node that represents a model in a workflow.

    The ModelNode can be used to load and execute a model as part of the
    workflow node. It initializes model configurations and performs
    model-related operations when called.
    """

    node_type = WorkflowNodeType.MODEL

    def __init__(
            self,
            node_id: str,
            opt_kwargs: dict,
            source_kwargs: dict,
            dep_opts: list,
    ) -> None:
        super().__init__(node_id, opt_kwargs, source_kwargs, dep_opts)
        read_model_configs([self.opt_kwargs])

    def compile(self) -> dict:
        return {
            "imports": "from agentscope.models import read_model_configs",
            "inits": f"read_model_configs([{self.opt_kwargs}])",
            "execs": "",
        }


class MsgNode(WorkflowNode):
    """
    A node that manages messaging within a workflow.

    MsgNode is responsible for handling messages, creating message objects,
    and performing message-related operations when the node is invoked.
    """

    node_type = WorkflowNodeType.MESSAGE

    def __init__(
            self,
            node_id: str,
            opt_kwargs: dict,
            source_kwargs: dict,
            dep_opts: list,
    ) -> None:
        super().__init__(node_id, opt_kwargs, source_kwargs, dep_opts)
        self.msg = Msg(**self.opt_kwargs)

    def __call__(self, x: dict = None) -> dict:
        return self.msg

    def compile(self) -> dict:
        return {
            "imports": "from agentscope.message import Msg",
            "inits": f"{DEFAULT_FLOW_VAR} = Msg"
                     f"({kwarg_converter(self.opt_kwargs)})",
            "execs": "",
        }


class DialogAgentNode(WorkflowNode):
    """
    A node representing a DialogAgent within a workflow.
    """

    node_type = WorkflowNodeType.AGENT

    def __init__(
            self,
            node_id: str,
            opt_kwargs: dict,
            source_kwargs: dict,
            dep_opts: list,
    ) -> None:
        super().__init__(node_id, opt_kwargs, source_kwargs, dep_opts)
        self.pipeline = DialogAgent(**self.opt_kwargs)

    def __call__(self, x: dict = None) -> dict:
        return self.pipeline(x)

    def compile(self) -> dict:
        return {
            "imports": "from agentscope.agents import DialogAgent",
            "inits": f"{self.var_name} = DialogAgent("
                     f"{kwarg_converter(self.opt_kwargs)})",
            "execs": f"{DEFAULT_FLOW_VAR} = {self.var_name}"
                     f"({DEFAULT_FLOW_VAR})",
        }


class UserAgentNode(WorkflowNode):
    """
    A node representing a UserAgent within a workflow.
    """

    node_type = WorkflowNodeType.AGENT

    def __init__(
            self,
            node_id: str,
            opt_kwargs: dict,
            source_kwargs: dict,
            dep_opts: list,
    ) -> None:
        super().__init__(node_id, opt_kwargs, source_kwargs, dep_opts)
        self.pipeline = UserAgent(**self.opt_kwargs)

    def __call__(self, x: dict = None) -> dict:
        return self.pipeline(x)

    def compile(self) -> dict:
        return {
            "imports": "from agentscope.agents import UserAgent",
            "inits": f"{self.var_name} = UserAgent("
                     f"{kwarg_converter(self.opt_kwargs)})",
            "execs": f"{DEFAULT_FLOW_VAR} = {self.var_name}"
                     f"({DEFAULT_FLOW_VAR})",
        }


class TextToImageAgentNode(WorkflowNode):
    """
    A node representing a TextToImageAgent within a workflow.
    """

    node_type = WorkflowNodeType.AGENT

    def __init__(
            self,
            node_id: str,
            opt_kwargs: dict,
            source_kwargs: dict,
            dep_opts: list,
    ) -> None:
        super().__init__(node_id, opt_kwargs, source_kwargs, dep_opts)
        self.pipeline = TextToImageAgent(**self.opt_kwargs)

    def __call__(self, x: dict = None) -> dict:
        return self.pipeline(x)

    def compile(self) -> dict:
        return {
            "imports": "from agentscope.agents import TextToImageAgent",
            "inits": f"{self.var_name} = TextToImageAgent("
                     f"{kwarg_converter(self.opt_kwargs)})",
            "execs": f"{DEFAULT_FLOW_VAR} = {self.var_name}"
                     f"({DEFAULT_FLOW_VAR})",
        }


class DictDialogAgentNode(WorkflowNode):
    """
    A node representing a DictDialogAgent within a workflow.
    """

    node_type = WorkflowNodeType.AGENT

    def __init__(
            self,
            node_id: str,
            opt_kwargs: dict,
            source_kwargs: dict,
            dep_opts: list,
    ) -> None:
        super().__init__(node_id, opt_kwargs, source_kwargs, dep_opts)
        self.pipeline = DictDialogAgent(**self.opt_kwargs)

    def __call__(self, x: dict = None) -> dict:
        return self.pipeline(x)

    def compile(self) -> dict:
        return {
            "imports": "from agentscope.agents import DictDialogAgent",
            "inits": f"{self.var_name} = DictDialogAgent("
                     f"{kwarg_converter(self.opt_kwargs)})",
            "execs": f"{DEFAULT_FLOW_VAR} = {self.var_name}"
                     f"({DEFAULT_FLOW_VAR})",
        }


class ReActAgentNode(WorkflowNode):
    """
    A node representing a ReActAgent within a workflow.
    """

    node_type = WorkflowNodeType.AGENT

    def __init__(
            self,
            node_id: str,
            opt_kwargs: dict,
            source_kwargs: dict,
            dep_opts: list,
    ) -> None:
        super().__init__(node_id, opt_kwargs, source_kwargs, dep_opts)
        # Build tools
        self.tools = []
        for tool in dep_opts:
            if not hasattr(tool, "service_func"):
                raise TypeError(f"{tool} must be tool!")
            self.tools.append(tool.service_func)
        self.pipeline = ReActAgent(tools=self.tools, **self.opt_kwargs)

    def __call__(self, x: dict = None) -> dict:
        return self.pipeline(x)

    def compile(self) -> dict:
        return {
            "imports": "from agentscope.agents import ReActAgent",
            "inits": f"{self.var_name} = ReActAgent"
                     f"({kwarg_converter(self.opt_kwargs)}, tools"
                     f"={deps_converter(self.dep_vars)})",
            "execs": f"{DEFAULT_FLOW_VAR} = {self.var_name}"
                     f"({DEFAULT_FLOW_VAR})",
        }


class MsgHubNode(WorkflowNode):
    """
    A node that serves as a messaging hub within a workflow.

    MsgHubNode is responsible for broadcasting announcements to participants
    and managing the flow of messages within a workflow's node.
    """

    node_type = WorkflowNodeType.PIPELINE

    def __init__(
            self,
            node_id: str,
            opt_kwargs: dict,
            source_kwargs: dict,
            dep_opts: list,
    ) -> None:
        super().__init__(node_id, opt_kwargs, source_kwargs, dep_opts)
        self.announcement = Msg(
            name=self.opt_kwargs["announcement"].get("name", "Host"),
            content=self.opt_kwargs["announcement"].get("content", "Welcome!"),
            role="system",
        )
        assert len(self.dep_opts) == 1 and hasattr(
            self.dep_opts[0],
            "pipeline",
        ), (
            "MsgHub members must be a list of length 1, with the first "
            "element being an instance of PipelineBaseNode"
        )

        self.pipeline = self.dep_opts[0]
        self.participants = get_all_agents(self.pipeline)
        self.participants_var = get_all_agents(self.pipeline, return_var=True)

    def __call__(self, x: dict = None) -> dict:
        with msghub(self.participants, announcement=self.announcement):
            x = self.pipeline(x)
        return x

    def compile(self) -> dict:
        announcement = (
            f'Msg(name="'
            f'{self.opt_kwargs["announcement"].get("name", "Host")}", '
            f'content="'
            f'{self.opt_kwargs["announcement"].get("content", "Host")}"'
            f', role="system")'
        )
        execs = f"""with msghub({deps_converter(self.participants_var)},
        announcement={announcement}):
        {DEFAULT_FLOW_VAR} = {self.dep_vars[0]}({DEFAULT_FLOW_VAR})
        """
        return {
            "imports": "from agentscope.msghub import msghub\n"
                       "from agentscope.message import Msg",
            "inits": "",
            "execs": execs,
        }


class PlaceHolderNode(WorkflowNode):
    """
    A placeholder node within a workflow.

    This node acts as a placeholder and can be used to pass through information
    or data without performing any significant operation.
    """

    node_type = WorkflowNodeType.PIPELINE

    def __init__(
            self,
            node_id: str,
            opt_kwargs: dict,
            source_kwargs: dict,
            dep_opts: list,
    ) -> None:
        super().__init__(node_id, opt_kwargs, source_kwargs, dep_opts)
        self.pipeline = placeholder

    def __call__(self, x: dict = None) -> dict:
        return self.pipeline(x)

    def compile(self) -> dict:
        return {
            "imports": "from agentscope.pipelines.functional import "
                       "placeholder",
            "inits": f"{self.var_name} = placeholder",
            "execs": f"{DEFAULT_FLOW_VAR} = {self.var_name}"
                     f"({DEFAULT_FLOW_VAR})",
        }


class SequentialPipelineNode(WorkflowNode):
    """
    A node representing a sequential node within a workflow.

    SequentialPipelineNode executes a series of operators or nodes in a
    sequence, where the output of one node is the input to the next.
    """

    node_type = WorkflowNodeType.PIPELINE

    def __init__(
            self,
            node_id: str,
            opt_kwargs: dict,
            source_kwargs: dict,
            dep_opts: list,
    ) -> None:
        super().__init__(node_id, opt_kwargs, source_kwargs, dep_opts)
        self.pipeline = SequentialPipeline(operators=self.dep_opts)

    def __call__(self, x: dict = None) -> dict:
        return self.pipeline(x)

    def compile(self) -> dict:
        return {
            "imports": "from agentscope.pipelines import SequentialPipeline",
            "inits": f"{self.var_name} = SequentialPipeline("
                     f"{deps_converter(self.dep_vars)})",
            "execs": f"{DEFAULT_FLOW_VAR} = {self.var_name}"
                     f"({DEFAULT_FLOW_VAR})",
        }


class ForLoopPipelineNode(WorkflowNode):
    """
    A node representing a for-loop structure in a workflow.

    ForLoopPipelineNode allows the execution of a pipeline node multiple times,
    iterating over a given set of inputs or a specified range.
    """

    node_type = WorkflowNodeType.PIPELINE

    def __init__(
            self,
            node_id: str,
            opt_kwargs: dict,
            source_kwargs: dict,
            dep_opts: list,
    ) -> None:
        super().__init__(node_id, opt_kwargs, source_kwargs, dep_opts)
        assert (
                len(self.dep_opts) == 1
        ), "ForLoopPipelineNode can only contain one PipelineNode."
        self.pipeline = ForLoopPipeline(
            loop_body_operators=self.dep_opts[0],
            **self.opt_kwargs,
        )

    def __call__(self, x: dict = None) -> dict:
        return self.pipeline(x)

    def compile(self) -> dict:
        return {
            "imports": "from agentscope.pipelines import ForLoopPipeline",
            "inits": f"{self.var_name} = ForLoopPipeline("
                     f"loop_body_operators="
                     f"{deps_converter(self.dep_vars)},"
                     f" {kwarg_converter(self.source_kwargs)})",
            "execs": f"{DEFAULT_FLOW_VAR} = {self.var_name}"
                     f"({DEFAULT_FLOW_VAR})",
        }


class WhileLoopPipelineNode(WorkflowNode):
    """
    A node representing a while-loop structure in a workflow.

    WhileLoopPipelineNode enables conditional repeated execution of a node
    node based on a specified condition.
    """

    node_type = WorkflowNodeType.PIPELINE

    def __init__(
            self,
            node_id: str,
            opt_kwargs: dict,
            source_kwargs: dict,
            dep_opts: list,
    ) -> None:
        super().__init__(node_id, opt_kwargs, source_kwargs, dep_opts)
        assert (
                len(self.dep_opts) == 1
        ), "WhileLoopPipelineNode can only contain one PipelineNode."
        self.pipeline = WhileLoopPipeline(
            loop_body_operators=self.dep_opts[0],
            **self.opt_kwargs,
        )

    def __call__(self, x: dict = None) -> dict:
        return self.pipeline(x)

    def compile(self) -> dict:
        return {
            "imports": "from agentscope.pipelines import WhileLoopPipeline",
            "inits": f"{self.var_name} = WhileLoopPipeline("
                     f"loop_body_operators="
                     f"{deps_converter(self.dep_vars)},"
                     f" {kwarg_converter(self.source_kwargs)})",
            "execs": f"{DEFAULT_FLOW_VAR} = {self.var_name}"
                     f"({DEFAULT_FLOW_VAR})",
        }


class IfElsePipelineNode(WorkflowNode):
    """
    A node representing an if-else conditional structure in a workflow.

    IfElsePipelineNode directs the flow of execution to different node
    nodes based on a specified condition.
    """

    node_type = WorkflowNodeType.PIPELINE

    def __init__(
            self,
            node_id: str,
            opt_kwargs: dict,
            source_kwargs: dict,
            dep_opts: list,
    ) -> None:
        super().__init__(node_id, opt_kwargs, source_kwargs, dep_opts)
        assert (
                0 < len(self.dep_opts) <= 2
        ), "IfElsePipelineNode must contain one or two PipelineNode."
        if len(self.dep_opts) == 1:
            self.pipeline = IfElsePipeline(
                if_body_operators=self.dep_opts[0],
                **self.opt_kwargs,
            )
        elif len(self.dep_opts) == 2:
            self.pipeline = IfElsePipeline(
                if_body_operators=self.dep_opts[0],
                else_body_operators=self.dep_opts[1],
                **self.opt_kwargs,
            )

    def __call__(self, x: dict = None) -> dict:
        return self.pipeline(x)

    def compile(self) -> dict:
        imports = "from agentscope.pipelines import IfElsePipeline"
        execs = f"{DEFAULT_FLOW_VAR} = {self.var_name}({DEFAULT_FLOW_VAR})"
        if len(self.dep_vars) == 1:
            return {
                "imports": imports,
                "inits": f"{self.var_name} = IfElsePipeline("
                         f"if_body_operators={self.dep_vars[0]})",
                "execs": execs,
            }
        elif len(self.dep_vars) == 2:
            return {
                "imports": imports,
                "inits": f"{self.var_name} = IfElsePipeline("
                         f"if_body_operators={self.dep_vars[0]}, "
                         f"else_body_operators={self.dep_vars[1]})",
                "execs": execs,
            }
        raise ValueError


class SwitchPipelineNode(WorkflowNode):
    """
    A node representing a switch-case structure within a workflow.

    SwitchPipelineNode routes the execution to different node nodes
    based on the evaluation of a specified key or condition.
    """

    node_type = WorkflowNodeType.PIPELINE

    def __init__(
            self,
            node_id: str,
            opt_kwargs: dict,
            source_kwargs: dict,
            dep_opts: list,
    ) -> None:
        super().__init__(node_id, opt_kwargs, source_kwargs, dep_opts)
        assert 0 < len(self.dep_opts), (
            "SwitchPipelineNode must contain at least " "one PipelineNode."
        )
        case_operators = {}
        self.case_operators_var = {}

        if len(self.dep_opts) == len(self.opt_kwargs["cases"]):
            # No default_operators provided
            default_operators = placeholder
            self.default_var_name = "placeholder"
        elif len(self.dep_opts) == len(self.opt_kwargs["cases"]) + 1:
            # default_operators provided
            default_operators = self.dep_opts.pop(-1)
            self.default_var_name = self.dep_vars.pop(-1)
        else:
            raise ValueError(
                f"SwitchPipelineNode deps {self.dep_opts} not matches "
                f"cases {self.opt_kwargs['cases']}.",
            )

        for key, value, var in zip(
                self.opt_kwargs["cases"],
                self.dep_opts,
                self.dep_vars,
        ):
            case_operators[key] = value.pipeline
            self.case_operators_var[key] = var
        self.opt_kwargs.pop("cases")
        self.source_kwargs.pop("cases")
        self.pipeline = SwitchPipeline(
            case_operators=case_operators,
            default_operators=default_operators,  # type: ignore[arg-type]
            **self.opt_kwargs,
        )

    def __call__(self, x: dict = None) -> dict:
        return self.pipeline(x)

    def compile(self) -> dict:
        imports = (
            "from agentscope.pipelines import SwitchPipeline\n"
            "from agentscope.pipelines.functional import placeholder"
        )
        execs = f"{DEFAULT_FLOW_VAR} = {self.var_name}({DEFAULT_FLOW_VAR})"
        return {
            "imports": imports,
            "inits": f"{self.var_name} = SwitchPipeline(case_operators="
                     f"{dict_converter(self.case_operators_var)}, "
                     f"default_operators={self.default_var_name},"
                     f" {kwarg_converter(self.source_kwargs)})",
            "execs": execs,
        }


class CopyNode(WorkflowNode):
    """
    A node that duplicates the output of another node in the workflow.

    CopyNode is used to replicate the results of a parent node and can be
    useful in workflows where the same output is needed for multiple
    subsequent operations.
    """

    node_type = WorkflowNodeType.COPY

    def __init__(
            self,
            node_id: str,
            opt_kwargs: dict,
            source_kwargs: dict,
            dep_opts: list,
    ) -> None:
        super().__init__(node_id, opt_kwargs, source_kwargs, dep_opts)
        assert len(self.dep_opts) == 1, "CopyNode can only have one parent!"
        self.pipeline = self.dep_opts[0]

    def __call__(self, x: dict = None) -> dict:
        return self.pipeline(x)

    def compile(self) -> dict:
        return {
            "imports": "",
            "inits": "",
            "execs": f"{DEFAULT_FLOW_VAR} = {self.dep_vars[0]}"
                     f"({DEFAULT_FLOW_VAR})",
        }


class BingSearchServiceNode(WorkflowNode):
    """
    Bing Search Node
    """

    node_type = WorkflowNodeType.SERVICE

    def __init__(
            self,
            node_id: str,
            opt_kwargs: dict,
            source_kwargs: dict,
            dep_opts: list,
    ) -> None:
        super().__init__(node_id, opt_kwargs, source_kwargs, dep_opts)
        self.service_func = ServiceFactory.get(bing_search, **self.opt_kwargs)

    def compile(self) -> dict:
        return {
            "imports": "from agentscope.service import ServiceFactory\n"
                       "from agentscope.service import bing_search",
            "inits": f"{self.var_name} = ServiceFactory.get(bing_search,"
                     f" {kwarg_converter(self.opt_kwargs)})",
            "execs": "",
        }


class GoogleSearchServiceNode(WorkflowNode):
    """
    Google Search Node
    """

    node_type = WorkflowNodeType.SERVICE

    def __init__(
            self,
            node_id: str,
            opt_kwargs: dict,
            source_kwargs: dict,
            dep_opts: list,
    ) -> None:
        super().__init__(node_id, opt_kwargs, source_kwargs, dep_opts)
        self.service_func = ServiceFactory.get(
            google_search,
            **self.opt_kwargs,
        )

    def compile(self) -> dict:
        return {
            "imports": "from agentscope.service import ServiceFactory\n"
                       "from agentscope.service import google_search",
            "inits": f"{self.var_name} = ServiceFactory.get(google_search,"
                     f" {kwarg_converter(self.opt_kwargs)})",
            "execs": "",
        }


class PythonServiceNode(WorkflowNode):
    """
    Execute python Node
    """

    node_type = WorkflowNodeType.SERVICE

    def __init__(
            self,
            node_id: str,
            opt_kwargs: dict,
            source_kwargs: dict,
            dep_opts: list,
    ) -> None:
        super().__init__(node_id, opt_kwargs, source_kwargs, dep_opts)
        self.service_func = ServiceFactory.get(execute_python_code)

    def compile(self) -> dict:
        return {
            "imports": "from agentscope.service import ServiceFactory\n"
                       "from agentscope.service import execute_python_code",
            "inits": f"{self.var_name} = ServiceFactory.get("
                     f"execute_python_code)",
            "execs": "",
        }


class ReadTextServiceNode(WorkflowNode):
    """
    Read Text Service Node
    """

    node_type = WorkflowNodeType.SERVICE

    def __init__(
            self,
            node_id: str,
            opt_kwargs: dict,
            source_kwargs: dict,
            dep_opts: list,
    ) -> None:
        super().__init__(node_id, opt_kwargs, source_kwargs, dep_opts)
        self.service_func = ServiceFactory.get(read_text_file)

    def compile(self) -> dict:
        return {
            "imports": "from agentscope.service import ServiceFactory\n"
                       "from agentscope.service import read_text_file",
            "inits": f"{self.var_name} = ServiceFactory.get(read_text_file)",
            "execs": "",
        }


class WriteTextServiceNode(WorkflowNode):
    """
    Write Text Service Node
    """

    node_type = WorkflowNodeType.SERVICE

    def __init__(
            self,
            node_id: str,
            opt_kwargs: dict,
            source_kwargs: dict,
            dep_opts: list,
    ) -> None:
        super().__init__(node_id, opt_kwargs, source_kwargs, dep_opts)
        self.service_func = ServiceFactory.get(write_text_file)

    def compile(self) -> dict:
        return {
            "imports": "from agentscope.service import ServiceFactory\n"
                       "from agentscope.service import write_text_file",
            "inits": f"{self.var_name} = ServiceFactory.get(write_text_file)",
            "execs": "",
        }


# 20240813
# 新增的开始节点
class StartNode(WorkflowNode):
    """
    开始节点代表输入参数列表.
    source_kwargs字段，用户定义了该节点的输入和输出变量名、类型、value

        "inputs": [],
        "outputs": [
            {
                "name": "poi",
                "type": "string",
                "desc": "节点功能中文描述",
                "object_schema": null,
                "list_schema": null,
                "value": {
                    "type": "generated",
                    "content": null
                }
            },
        ],
        "settings": {}

    """

    node_type = WorkflowNodeType.START

    def __init__(
            self,
            node_id: str,
            opt_kwargs: dict,
            source_kwargs: dict,
            dep_opts: list,
            dag_obj: Optional[ASDiGraph] = None,
    ) -> None:
        super().__init__(node_id, opt_kwargs, source_kwargs, dep_opts)
        # opt_kwargs 包含用户定义的节点输入变量
        self.input_params = {}
        self.output_params = {}
        # init --> running -> success/failed
        self.running_status = WorkflowNodeStatus.INIT
        self.running_message = ""
        self.dag_obj = dag_obj
        self.dag_id = ""
        if self.dag_obj:
            self.dag_id = self.dag_obj.uuid

    def __call__(self, *args, **kwargs):
        # 注意，这里是开始节点，所以不需要判断入参是否为空

        self.running_status = WorkflowNodeStatus.RUNNING
        try:
            self.run(*args, **kwargs)
            self.running_status = WorkflowNodeStatus.SUCCESS
            return self.output_params
        except Exception as err:
            logger.error(f'{traceback.format_exc()}')
            self.running_status = WorkflowNodeStatus.FAILED
            self.running_message = f'{repr(err)}'
            return {}

    def run(self, *args, **kwargs):
        if len(kwargs) == 0:
            raise Exception("input param dict kwargs empty")

        # 1. 建立参数映射
        self.dag_obj.init_params_pool_with_running_node(self.node_id)

        params_dict = self.opt_kwargs
        for i, param_spec in enumerate(params_dict['outputs']):
            # param_spec 举例
            # {
            #     "name": "apiKeywords",
            #     "type": "string",
            #     "desc": "",
            #     "required": false,
            #     "value": {
            #         "type": "ref",
            #         "content": {
            #             "ref_node_id": "4daf0d1a33af497e9819fe515133eb5f",
            #             "ref_var_name": "keywords"
            #         }
            #     },
            #     "object_schema": null,
            #     "list_schema": null,
            # }

            # 防御性措施
            if param_spec.get('name', '') == '':
                continue

            param_one_dict = self.dag_obj.generate_node_param_real(param_spec)
            self.output_params |= param_one_dict

        # 2. 解析实际的取值
        for k, v in kwargs.items():
            if k not in self.output_params:
                continue
            self.output_params[k] = v

        self.dag_obj.update_params_pool_with_running_node(self.node_id, self.output_params)
        logger.info(
            f"{self.var_name}, run success, input params: {self.input_params}, output params: {self.output_params}")
        return self.output_params

    def __str__(self) -> str:
        message = f'dag: {self.dag_id}, {self.var_name}'
        return message

    def compile(self) -> dict:
        """
        入参在这里初始化.
        Returns:

        """
        # 检查参数格式是否正确
        logger.info(f"{self.var_name}, compile param: {self.opt_kwargs}")
        if 'inputs' not in self.opt_kwargs:
            raise Exception("inputs key not found")
        if 'outputs' not in self.opt_kwargs:
            raise Exception("outputs key not found")
        if 'settings' not in self.opt_kwargs:
            raise Exception("settings key not found")

        if not isinstance(self.opt_kwargs['inputs'], list):
            raise Exception(f"inputs:{self.opt_kwargs['inputs']} type is not list")
        if not isinstance(self.opt_kwargs['outputs'], list):
            raise Exception(f"outputs:{self.opt_kwargs['outputs']} type is not list")
        if not isinstance(self.opt_kwargs['settings'], dict):
            raise Exception(f"settings:{self.opt_kwargs['settings']} type is not dict")

        return {
            "imports": "",
            "inits": "",
            "execs": "",
        }


class EndNode(WorkflowNode):
    """
    结束节点只有输入没有输出
    """
    node_type = WorkflowNodeType.END

    def __init__(self,
                 node_id: str,
                 opt_kwargs: dict,
                 source_kwargs: dict,
                 dep_opts: list,
                 dag_obj: Optional[ASDiGraph] = None,
                 ) -> None:
        super().__init__(node_id, opt_kwargs, source_kwargs, dep_opts)
        # opt_kwargs 包含用户定义的节点输入变量
        self.input_params = {}
        self.output_params = {}
        # init --> running -> success/failed:xxx
        self.running_status = WorkflowNodeStatus.INIT
        self.running_message = ""
        self.dag_obj = dag_obj
        self.dag_id = ""
        if self.dag_obj:
            self.dag_id = self.dag_obj.uuid

    def __call__(self, *args, **kwargs) -> dict:
        # 判断当前节点的运行状态
        predecessors_list = list(self.dag_obj.predecessors(self.node_id))
        # 0. 节点无前驱节点且输入参数为空时，节点为等待状态
        if len(kwargs) == 0 and not predecessors_list:
            self.running_status = WorkflowNodeStatus.INIT
            return {}
        # 1. 节点前驱节点存在失败节点, 节点为等待状态
        if any(
                self.dag_obj.nodes[node]["opt"].running_status in WorkflowNodeStatus.FAILED for node
                in predecessors_list):
            self.running_status = WorkflowNodeStatus.INIT
            return {}
        try:
            self.run(*args, **kwargs)
            self.running_status = WorkflowNodeStatus.SUCCESS
            # 尾节点，没有输出值
            return {}
        except Exception as err:
            logger.error(f'{traceback.format_exc()}')
            self.running_status = WorkflowNodeStatus.FAILED
            self.running_message = f'{repr(err)}'
            return {}

    def run(self, *args, **kwargs):
        self.dag_obj.init_params_pool_with_running_node(self.node_id)

        # 1. 建立参数映射
        params_dict = self.opt_kwargs
        for i, param_spec in enumerate(params_dict['inputs']):
            # param_spec 举例
            # {
            #     "name": "apiKeywords",
            #     "type": "string",
            #     "desc": "",
            #     "required": false,
            #     "value": {
            #         "type": "ref",
            #         "content": {
            #             "ref_node_id": "4daf0d1a33af497e9819fe515133eb5f",
            #             "ref_var_name": "keywords"
            #         }
            #     },
            #     "object_schema": null,
            #     "list_schema": null,
            # }

            # 防御性措施
            if param_spec.get('name', '') == '':
                continue

            param_one_dict = self.dag_obj.generate_node_param_real(param_spec)
            self.input_params |= param_one_dict

        # 注意，尾节点不需要再放到全局变量池里
        logger.info(
            f"{self.var_name}, run success, input params: {self.input_params}, output params: {self.output_params}")
        return self.input_params

    def __str__(self) -> str:
        message = f'dag: {self.dag_id}, {self.var_name}'
        return message

    def compile(self) -> dict:
        # 检查参数格式是否正确
        logger.info(f"{self.var_name}, compile param: {self.opt_kwargs}")
        params_dict = self.opt_kwargs

        # 检查参数格式是否正确
        if 'inputs' not in params_dict:
            raise Exception("inputs key not found")
        if 'outputs' not in params_dict:
            raise Exception("outputs key not found")
        if 'settings' not in params_dict:
            raise Exception("settings key not found")

        if not isinstance(params_dict['inputs'], list):
            raise Exception(f"inputs:{params_dict['inputs']} type is not list")
        if not isinstance(params_dict['outputs'], list):
            raise Exception(f"outputs:{params_dict['outputs']} type is not list")
        if not isinstance(params_dict['settings'], dict):
            raise Exception(f"settings:{params_dict['settings']} type is not dict")

        return {
            "imports": "",
            "inits": "",
            "execs": "",
        }


class PythonServiceUserTypingNode(WorkflowNode):
    """
    Execute Python Node,支持用户输入
    使用 source_kwargs 获取用户输入的 Python 代码
    这个代码将作为 execute_python_code 函数的 code 参数传入。
    """

    node_type = WorkflowNodeType.PYTHON

    def __init__(
            self,
            node_id: str,
            opt_kwargs: dict,
            source_kwargs: dict,
            dep_opts: list,
            dag_obj: Optional[ASDiGraph] = None,
    ) -> None:
        super().__init__(node_id, opt_kwargs, source_kwargs, dep_opts)
        # opt_kwargs 包含用户定义的节点输入变量
        self.input_params = {'params': {}}
        self.output_params = {}
        self.output_params_spec = {}
        # init --> running -> success/failed:xxx
        self.running_status = WorkflowNodeStatus.INIT
        self.running_message = ""
        self.dag_obj = dag_obj
        self.dag_id = ""
        if self.dag_obj:
            self.dag_id = self.dag_obj.uuid

        self.python_code = ""

    def __call__(self, *args, **kwargs) -> dict:
        # 判断当前节点的运行状态
        self.running_status, is_running = self.dag_obj.confirm_current_node_running_status(self.node_id, kwargs)
        if not is_running:
            return {}

        try:
            self.run(*args, **kwargs)
            self.running_status = WorkflowNodeStatus.SUCCESS
            return self.output_params
        except Exception as err:
            logger.error(f'{traceback.format_exc()}')
            self.running_status = WorkflowNodeStatus.FAILED
            self.running_message = f'{repr(err)}'
            return {}

    def run(self, *args, **kwargs):
        self.dag_obj.init_params_pool_with_running_node(self.node_id)

        # 1. 建立参数映射
        params_dict = self.opt_kwargs
        for i, param_spec in enumerate(params_dict['inputs']):
            # param_spec 举例
            # {
            #     "name": "apiKeywords",
            #     "type": "string",
            #     "desc": "",
            #     "required": false,
            #     "value": {
            #         "type": "ref",
            #         "content": {
            #             "ref_node_id": "4daf0d1a33af497e9819fe515133eb5f",
            #             "ref_var_name": "keywords"
            #         }
            #     },
            #     "object_schema": null,
            #     "list_schema": null,
            # }

            # 防御性措施
            if param_spec.get('name', '') == '':
                continue

            param_one_dict = self.dag_obj.generate_node_param_real(param_spec)
            self.input_params['params'] |= param_one_dict

        logger.info(f"self.input_params, {self.input_params}")
        logger.info(f"self.output_params, {self.output_params}")
        logger.info(f"self.node_id, {self.node_id}")
        # 2. 运行python解释器代码        # 单个节点调试运行场景"
        if len(self.output_params_spec) == 0:
            if len(kwargs) > 0 and len(self.input_params['params']) > 0:
                raise Exception("single node debug run, but real input param not empty list")
            self.input_params['params'] = kwargs

        response = execute_python_code(
            self.python_code, use_docker=False, extra_readonly_input_params=self.input_params['params'])
        if response.status == service_status.ServiceExecStatus.ERROR:
            raise Exception(str(response.content))

        # 单个节点调试场景，不解析出参，直接返回调试的结果
        if len(self.output_params_spec) == 0:
            self.output_params = response.content
            logger.info(f"{self.var_name}, run success,"
                        f"input params: {self.input_params}, output params: {self.output_params}")
            return self.output_params

        self.output_params = response.content
        # 检查返回值是否对应
        for k in self.output_params_spec:
            if k not in self.output_params:
                raise Exception(f"user defined output parameter '{k}' not found in 'output_params' code return value:"
                                f"{self.output_params}")

        self.dag_obj.update_params_pool_with_running_node(self.node_id, self.output_params)
        logger.info(
            f"{self.var_name}, run success, input params: {self.input_params}, output params: {self.output_params}")
        return self.output_params

    def __str__(self) -> str:
        message = f'dag: {self.dag_id}, {self.var_name}'
        return message

    def compile(self) -> dict:
        # 检查参数格式是否正确
        logger.info(f"{self.var_name}, compile param: {self.opt_kwargs}")
        params_dict = self.opt_kwargs

        # 检查参数格式是否正确
        if 'inputs' not in params_dict:
            raise Exception("inputs key not found")
        if 'outputs' not in params_dict:
            raise Exception("outputs key not found")
        if 'settings' not in params_dict:
            raise Exception("settings key not found")

        if not isinstance(params_dict['inputs'], list):
            raise Exception(f"inputs:{params_dict['inputs']} type is not list")
        if not isinstance(params_dict['outputs'], list):
            raise Exception(f"outputs:{params_dict['outputs']} type is not list")
        if not isinstance(params_dict['settings'], dict):
            raise Exception(f"settings:{params_dict['settings']} type is not dict")

        base64_python_code = params_dict['settings'].get('code', None)
        if not base64_python_code:
            raise Exception("python code empty")

        isBase64 = False
        try:
            base64.b64encode(base64.b64decode(base64_python_code)) == base64_python_code
            isBase64 = True
        except Exception:
            isBase64 = False
            raise Exception("python code str not base64")

        self.python_code = base64.b64decode(base64_python_code).decode('utf-8')
        if self.python_code == "":
            raise Exception("python code empty")

        for i, param_spec in enumerate(params_dict['outputs']):
            # param_spec 举例
            # {
            #     "name": "apiKeywords",
            #     "type": "string",
            #     "desc": "",
            #     "required": false,
            #     "value": {
            #         "type": "ref",
            #         "content": {
            #             "ref_node_id": "4daf0d1a33af497e9819fe515133eb5f",
            #             "ref_var_name": "keywords"
            #         }
            #     },
            #     "object_schema": null,
            #     "list_schema": null,
            # }

            # 防御性措施
            if param_spec.get('name', '') == '':
                continue

            param_one_dict = ASDiGraph.generate_node_param_spec(param_spec)
            self.output_params_spec = param_one_dict
        return {
            "imports": "",
            "inits": "",
            "execs": "",
        }


# 新增通用api调用节点
# api请求的所需参数从setting中获取
class ApiNode(WorkflowNode):
    """
    API GET Node for executing HTTP requests using the api_request function.
    """

    node_type = WorkflowNodeType.API

    def __init__(
            self,
            node_id: str,
            opt_kwargs: dict,
            source_kwargs: dict,
            dep_opts: list,
            dag_obj: Optional[ASDiGraph] = None,
    ) -> None:
        super().__init__(node_id, opt_kwargs, source_kwargs, dep_opts)
        self.input_params = {}
        self.output_params = {}
        self.output_params_spec = {}
        # init --> running -> success/failed:xxx
        self.running_status = WorkflowNodeStatus.INIT
        self.running_message = ''
        self.dag_obj = dag_obj
        self.dag_id = ""
        if self.dag_obj:
            self.dag_id = self.dag_obj.uuid

        # GET or POST
        self.api_type = ""
        self.api_url = ""
        self.api_header = {}
        self.input_params_for_query = {}
        self.input_params_for_body = {}

    def compile(self) -> dict:
        # 检查参数格式是否正确
        logger.info(f"{self.var_name}, compile param: {self.opt_kwargs}")
        params_dict = self.opt_kwargs

        # 检查参数格式是否正确
        if 'inputs' not in params_dict:
            raise Exception("inputs key not found")
        if 'outputs' not in params_dict:
            raise Exception("outputs key not found")
        if 'settings' not in params_dict:
            raise Exception("settings key not found")

        if not isinstance(params_dict['inputs'], list):
            raise Exception(f"inputs:{params_dict['inputs']} type is not list")
        if not isinstance(params_dict['outputs'], list):
            raise Exception(f"outputs:{params_dict['outputs']} type is not list")
        if not isinstance(params_dict['settings'], dict):
            raise Exception(f"settings:{params_dict['settings']} type is not dict")

        if 'url' not in params_dict['settings']:
            raise Exception("url key not found in settings")
        if 'http_method' not in params_dict['settings']:
            raise Exception("http_method key not found in settings")
        if 'headers' not in params_dict['settings']:
            raise Exception("headers key not found in settings")

        self.api_type = params_dict['settings']['http_method']
        self.api_url = params_dict['settings']['url']
        self.api_header = params_dict['settings']['headers']

        if self.api_type not in {"GET", "POST"}:
            raise Exception("http type: {self.api_type} invalid")
        if self.api_url == '':
            raise Exception("http url empty")
        if not isinstance(self.api_header, dict):
            raise Exception(f"header:{self.api_header} type is not dict")

        for i, param_spec in enumerate(params_dict['inputs']):
            # 防御性措施
            if param_spec.get('name', '') == '':
                continue

            param_spec.setdefault('extra', {})
            # 防御性措施
            param_spec['extra'].setdefault('location', 'query')
            if param_spec['extra']['location'] not in {'query', 'body'}:
                raise Exception("input param: {param_spec} extra-location not found('query' or 'body')")

        for i, param_spec in enumerate(params_dict['outputs']):
            # param_spec 举例
            # {
            #     "name": "apiKeywords",
            #     "type": "string",
            #     "desc": "",
            #     "required": false,
            #     "value": {
            #         "type": "ref",
            #         "content": {
            #             "ref_node_id": "4daf0d1a33af497e9819fe515133eb5f",
            #             "ref_var_name": "keywords"
            #         }
            #     },
            #     "object_schema": null,
            #     "list_schema": null,
            # }

            # 防御性措施
            if param_spec.get('name', '') == '':
                continue

            param_one_dict = ASDiGraph.generate_node_param_spec(param_spec)
            self.output_params_spec = param_one_dict

        return {
            "imports": "",
            "inits": "",
            "execs": "",
        }

    def __call__(self, *args, **kwargs):
        # 判断当前节点的运行状态
        self.running_status, is_running = self.dag_obj.confirm_current_node_running_status(self.node_id, kwargs)
        if not is_running:
            return {}

        try:
            self.run(*args, **kwargs)
            self.running_status = WorkflowNodeStatus.SUCCESS
            return self.output_params
        except Exception as err:
            logger.error(f'{traceback.format_exc()}')
            self.running_status = WorkflowNodeStatus.FAILED
            self.running_message = f'{repr(err)}'
            return {}

    def run(self, *args, **kwargs):
        self.dag_obj.init_params_pool_with_running_node(self.node_id)

        # 1. 建立参数映射
        params_dict = self.opt_kwargs
        for i, param_spec in enumerate(params_dict['inputs']):
            # param_spec 举例
            # {
            #     "name": "apiKeywords",
            #     "type": "string",
            #     "desc": "",
            #     "required": false,
            #     "value": {
            #         "type": "ref",
            #         "content": {
            #             "ref_node_id": "4daf0d1a33af497e9819fe515133eb5f",
            #             "ref_var_name": "keywords"
            #         }
            #     },
            #     "object_schema": null,
            #     "list_schema": null,
            #     "extra": {
            #         "location": "query"
            #     }
            # }

            # 防御性措施
            if param_spec.get('name', '') == '':
                continue

            param_one_dict_for_query, param_one_dict_for_body \
                = self.dag_obj.generate_node_param_real_for_api_input(param_spec)

            if not isinstance(param_one_dict_for_query, dict):
                raise Exception("input param: {param_one_dict_for_query} type not dict")
            if not isinstance(param_one_dict_for_body, dict):
                raise Exception("input param: {param_one_dict_for_body} type not dict")

            self.input_params |= param_one_dict_for_query
            self.input_params |= param_one_dict_for_body
            self.input_params_for_query |= param_one_dict_for_query
            self.input_params_for_body |= param_one_dict_for_body

        # 2. 使用 api_request 函数进行 API 请求设置
        response = api_request(url=self.api_url, method=self.api_type, headers=self.api_header,
                               params=self.input_params_for_query, json=self.input_params_for_body)
        if response.status == service_status.ServiceExecStatus.ERROR:
            raise Exception(str(response.content))

        # 3. 拆包解析
        # 单个节点调试场景，不解析出参，直接返回调试的结果
        if len(self.output_params_spec) == 0:
            logger.info(
                f"{self.var_name}, run success, input params: {self.input_params}, output params: {self.output_params}")
            self.output_params = response.content
            return self.output_params

        self.output_params = response.content
        # 检查返回值是否对应
        for k in self.output_params_spec:
            if k not in self.output_params:
                logger.error(f"api response: {self.output_params}")
                raise Exception(f"user defined output parameter {k} not found in api response")

        self.dag_obj.update_params_pool_with_running_node(self.node_id, self.output_params)
        logger.info(
            f"{self.var_name}, run success, input params: {self.input_params}, output params: {self.output_params}")
        return self.output_params


class LLMNode(WorkflowNode):
    """
    LLM Node.
    """

    node_type = WorkflowNodeType.LLM

    def __init__(
            self,
            node_id: str,
            opt_kwargs: dict,
            source_kwargs: dict,
            dep_opts: list,
            dag_obj: Optional[ASDiGraph] = None,
    ) -> None:
        super().__init__(node_id, opt_kwargs, source_kwargs, dep_opts)
        self.input_params = {}
        self.output_params = {}
        self.output_params_spec = {}
        # init --> running -> success/failed:xxx
        self.running_status = WorkflowNodeStatus.INIT
        self.running_message = ''
        self.dag_obj = dag_obj
        self.dag_id = ""
        if self.dag_obj:
            self.dag_id = self.dag_obj.uuid

        # GET or POST
        self.api_url = ""
        self.api_header = {}
        self.input_params_for_query = {}
        self.input_params_for_body = {}

    def compile(self) -> dict:
        # 检查参数格式是否正确
        logger.info(f"{self.var_name}, compile param: {self.opt_kwargs}")
        params_dict = self.opt_kwargs
        # 检查参数格式是否正确
        if 'inputs' not in params_dict:
            raise Exception("inputs key not found")
        if 'outputs' not in params_dict:
            raise Exception("outputs key not found")
        if 'settings' not in params_dict:
            raise Exception("settings key not found")

        if not isinstance(params_dict['inputs'], list):
            raise Exception(f"inputs:{params_dict['inputs']} type is not list")
        if not isinstance(params_dict['outputs'], list):
            raise Exception(f"outputs:{params_dict['outputs']} type is not list")
        if not isinstance(params_dict['settings'], dict):
            raise Exception(f"settings:{params_dict['settings']} type is not dict")

        if 'model' not in params_dict['settings']:
            raise Exception("model not found in settings")
        if 'headers' not in params_dict['settings']:
            raise Exception("headers key not found in settings")

        # 调用内部函数，将LLM Model请求参数组合为大模型可以识别的参数
        params_dict = LLMNode.convert_llm_request(params_dict)
        self.api_url = params_dict['settings']['url']
        self.api_header = params_dict['settings']['headers']
        if not isinstance(self.api_header, dict):
            raise Exception(f"header:{self.api_header} type is not dict")

        self.api_header = {
            "Authorization": f"Bearer {LLM_TOKEN}"
        }

        for i, param_spec in enumerate(params_dict['inputs']):
            param_spec.setdefault('extra', {})
            param_spec['extra'].setdefault('location', '')
            if param_spec['extra'].get('location', '') not in {'query', 'body'}:
                raise Exception("input param: {param_spec} extra-location not found('query' or 'body')")

        for i, param_spec in enumerate(params_dict['outputs']):
            param_one_dict = ASDiGraph.generate_node_param_spec(param_spec)
            self.output_params_spec = param_one_dict

        return {
            "imports": "",
            "inits": "",
            "execs": "",
        }

    def __call__(self, *args, **kwargs):
        # 判断当前节点的运行状态
        self.running_status, is_running = self.dag_obj.confirm_current_node_running_status(self.node_id, kwargs)
        if not is_running:
            return {}

        try:
            self.run(*args, **kwargs)
            self.running_status = WorkflowNodeStatus.SUCCESS
            return self.output_params
        except Exception as err:
            logger.error(f'{traceback.format_exc()}')
            self.running_status = WorkflowNodeStatus.FAILED
            self.running_message = f'{repr(err)}'
            return {}

    def run(self, *args, **kwargs):
        self.dag_obj.init_params_pool_with_running_node(self.node_id)

        # 1. 建立参数映射
        params_dict = self.opt_kwargs

        for i, param_spec in enumerate(params_dict['inputs']):
            param_one_dict_for_query, param_one_dict_for_body \
                = self.dag_obj.generate_node_param_real_for_api_input(param_spec)

            if not isinstance(param_one_dict_for_query, dict):
                raise Exception("input param: {param_one_dict_for_query} type not dict")
            if not isinstance(param_one_dict_for_body, dict):
                raise Exception("input param: {param_one_dict_for_body} type not dict")

            self.input_params |= param_one_dict_for_query
            self.input_params |= param_one_dict_for_body
            self.input_params_for_query |= param_one_dict_for_query
            self.input_params_for_body |= param_one_dict_for_body

        # 大模型参数校验
        temperature = self.input_params_for_body.get('temperature', None)
        if not (0 <= temperature <= 1):
            raise Exception("温度参数错误，应该在[0,1]之间")
        top_p = self.input_params_for_body.get('top_p', None)
        if not (0 <= top_p <= 1):
            raise Exception("多样性参数错误，应该在[0,1]之间")
        repetition_penalty = self.input_params_for_body.get('repetition_penalty', None)
        if not (1 <= repetition_penalty <= 1.3):
            raise Exception("重复惩罚参数错误，应该在[1,1.3]之间")
        messages = self.input_params_for_body.get('messages', None)
        if not messages:
            raise Exception("大模型节点提示词为空,请填写提示词")
        if 'headers' not in params_dict['settings']:
            raise Exception("headers key not found in settings")
        # prompt提示词拼接
        self.generate_prompt(*args, **kwargs)
        # 2. 使用 api_request 函数进行 API 请求设置
        response = api_request_for_big_model(url=self.api_url, method='POST', headers=self.api_header,
                                             data=self.input_params_for_body)
        if response.status == service_status.ServiceExecStatus.ERROR:
            raise Exception(str(response.content))
        if response.content is None:
            raise Exception("Can not get response from selected model")
        # 3. 拆包解析
        self.output_params = LLMNode.convert_llm_response(response.content)
        # 检查返回值是否对应
        for k in self.output_params_spec:
            if k not in self.output_params:
                logger.error(f"api response: {self.output_params}")
                raise Exception(f"user defined output parameter {k} not found in api response")

        self.dag_obj.update_params_pool_with_running_node(self.node_id, self.output_params)
        logger.info(
            f"{self.var_name}, run success, input params: {self.input_params}, output params: {self.output_params}")
        return self.output_params

    def generate_prompt(self, *args, **kwargs):
        messages = self.input_params_for_body["messages"]
        try:
            updated_messages = messages.format(**self.input_params_for_body)
            # 将入参补全为模型可识别形式
            self.input_params_for_body["messages"] = [{'role': 'user', 'content': updated_messages}]
        except KeyError as e:
            error_message = f"Missing key for formatting: {e}"
            raise ValueError(error_message)
        return self.input_params_for_body

    @staticmethod
    def convert_llm_request(origin_params_dict: dict) -> dict:
        # 将name为Input替换为LLM识别的messages
        for item in origin_params_dict['inputs']:
            if item['name'] == 'input':
                item['name'] = 'messages'
        # 替换model字段为对应model的url
        # TODO 支持多个模型后，需要提取函数进行配置每个模型
        if origin_params_dict['settings']['model'] == 'unicom-70b-chat':
            origin_params_dict['settings'].pop('model')

            origin_params_dict['settings']['url'] = LLM_URL
        else:
            raise Exception(f"{origin_params_dict['settings']['model']} is not supported")
        return origin_params_dict

    @staticmethod
    def convert_llm_response(origin_params_dict: dict) -> dict:
        content = origin_params_dict['data']['choices'][0]['message']['content']
        updated_params_dict = {'content': content}
        return updated_params_dict


class SwitchNode(WorkflowNode):
    """
    A node representing a switch-case structure within a workflow.
    SwitchPipelineNode routes the execution to different node nodes
    based on the evaluation of a specified key or condition.
    """

    node_type = WorkflowNodeType.SWITCH

    def __init__(
            self,
            node_id: str,
            opt_kwargs: dict,
            source_kwargs: dict,
            dep_opts: list,
            dag_obj: Optional[ASDiGraph] = None,
    ) -> None:
        super().__init__(node_id, opt_kwargs, source_kwargs, dep_opts)
        # opt_kwargs 包含用户定义的节点输入变量
        self.input_params = {}
        self.output_params = {}
        # init --> running -> success/failed:xxx
        self.running_status = WorkflowNodeStatus.INIT
        self.running_message = ""
        self.dag_obj = dag_obj
        self.dag_id = ""
        if self.dag_obj:
            self.dag_id = self.dag_obj.uuid

    def __call__(self, *args, **kwargs):
        # 判断当前节点的运行状态
        self.running_status, is_running = self.dag_obj.confirm_current_node_running_status(self.node_id, kwargs)
        if not is_running:
            return {}

        try:
            self.run(*args, **kwargs)
            self.running_status = WorkflowNodeStatus.SUCCESS
            self.running_message = f'pass to branch {self.dag_obj.selected_branch}'
            return self.output_params
        except Exception as err:
            logger.error(f'{traceback.format_exc()}')
            self.running_status = WorkflowNodeStatus.FAILED
            self.running_message = f'{repr(err)}'
            return {}

    def compile(self) -> dict:
        # 检查参数格式是否正确
        logger.info(f"{self.var_name}, compile param: {self.opt_kwargs}")
        params_dict = self.opt_kwargs

        # 检查参数格式是否正确
        if 'inputs' not in params_dict:
            raise Exception("inputs key not found")
        if 'outputs' not in params_dict:
            raise Exception("outputs key not found")
        if 'settings' not in params_dict:
            raise Exception("settings key not found")

        if not isinstance(params_dict['inputs'], list):
            raise Exception(f"inputs:{params_dict['inputs']} type is not list")
        if not isinstance(params_dict['outputs'], list):
            raise Exception(f"outputs:{params_dict['outputs']} type is not list")
        if not isinstance(params_dict['settings'], dict):
            raise Exception(f"settings:{params_dict['settings']} type is not dict")

        for item in params_dict["inputs"]:
            if "logic" not in item:
                raise Exception("logic not found in inputs")
            elif "target_node_id" not in item:
                raise Exception("target node not found in inputs")
            elif "conditions" not in item:
                raise Exception("conditions not found in inputs")

        return {
            "imports": "",
            "inits": "",
            "execs": "",
        }

    def run(self, *args, **kwargs):
        params_pool = self.dag_obj.params_pool
        params_pool.setdefault(self.node_id, {})

        # 1. 建立参数映射
        params_dict = self.opt_kwargs
        for branch, param_spec in enumerate(params_dict['inputs']):
            param_one_dict = self.dag_obj.generate_node_param_real_for_switch_input(param_spec)
            # 2. 运行SwitchNode转换代码，将json转换为对应判断
            self.dag_obj.generate_and_execute_condition_python_code(self.node_id, param_one_dict, branch)
        return self.output_params


class RAGNode(WorkflowNode):
    """
    RAG Node.
    """

    node_type = WorkflowNodeType.RAG

    def __init__(
            self,
            node_id: str,
            opt_kwargs: dict,
            source_kwargs: dict,
            dep_opts: list,
            dag_obj: Optional[ASDiGraph] = None,
    ) -> None:
        super().__init__(node_id, opt_kwargs, source_kwargs, dep_opts)
        self.input_params = {}
        self.output_params = {}
        self.output_params_spec = {}
        # init --> running -> success/failed:xxx
        self.running_status = WorkflowNodeStatus.INIT
        self.running_message = ''
        self.dag_obj = dag_obj
        self.dag_id = ""
        if self.dag_obj:
            self.dag_id = self.dag_obj.uuid

        # GET or POST
        self.api_url = ""
        self.userId = ""
        self.knowledgeBase = []
        self.api_header = {}
        self.input_params_for_query = {}
        self.input_params_for_body = {}

    def compile(self) -> dict:
        # 检查参数格式是否正确
        logger.info(f"{self.var_name}, compile param: {self.opt_kwargs}")
        params_dict = self.opt_kwargs
        # 检查参数格式是否正确
        if 'inputs' not in params_dict:
            raise Exception("inputs key not found")
        if 'outputs' not in params_dict:
            raise Exception("outputs key not found")
        if 'settings' not in params_dict:
            raise Exception("settings key not found")

        if not isinstance(params_dict['inputs'], list):
            raise Exception(f"inputs:{params_dict['inputs']} type is not list")
        if not isinstance(params_dict['outputs'], list):
            raise Exception(f"outputs:{params_dict['outputs']} type is not list")
        if not isinstance(params_dict['settings'], dict):
            raise Exception(f"settings:{params_dict['settings']} type is not dict")

        # 调用内部函数，将请求参数组合为RAG可以识别的参数
        params_dict = RAGNode.convert_rag_request(params_dict)
        self.api_header = params_dict['settings']['headers']
        self.api_url = params_dict['settings']['url']
        self.knowledgeBase = params_dict['settings']['knowledgeBase']
        self.userId = params_dict['settings']['userId']
        if not isinstance(self.api_header, dict):
            raise Exception(f"header:{self.api_header} type is not dict")

        for i, param_spec in enumerate(params_dict['inputs']):
            param_spec.setdefault('extra', {})
            param_spec['extra'].setdefault('location', '')
            if param_spec['extra'].get('location', '') not in {'query', 'body'}:
                raise Exception("input param: {param_spec} extra-location not found('query' or 'body')")

        for i, param_spec in enumerate(params_dict['outputs']):
            param_one_dict = ASDiGraph.generate_node_param_spec(param_spec)
            self.output_params_spec = param_one_dict

        return {
            "imports": "",
            "inits": "",
            "execs": "",
        }

    def __call__(self, *args, **kwargs):
        # 判断当前节点的运行状态
        self.running_status, is_running = self.dag_obj.confirm_current_node_running_status(self.node_id, kwargs)
        if not is_running:
            return {}

        try:
            self.run(*args, **kwargs)
            self.running_status = WorkflowNodeStatus.SUCCESS
            return self.output_params
        except Exception as err:
            logger.error(f'{traceback.format_exc()}')
            self.running_status = WorkflowNodeStatus.FAILED
            self.running_message = f'{repr(err)}'
            return {}

    def run(self, *args, **kwargs):
        self.dag_obj.init_params_pool_with_running_node(self.node_id)

        # 1. 建立参数映射
        params_dict = self.opt_kwargs

        for i, param_spec in enumerate(params_dict['inputs']):
            param_one_dict_for_query, param_one_dict_for_body \
                = self.dag_obj.generate_node_param_real_for_api_input(param_spec)

            if not isinstance(param_one_dict_for_query, dict):
                raise Exception("input param: {param_one_dict_for_query} type not dict")
            if not isinstance(param_one_dict_for_body, dict):
                raise Exception("input param: {param_one_dict_for_body} type not dict")

            self.input_params |= param_one_dict_for_query
            self.input_params |= param_one_dict_for_body
            self.input_params_for_query |= param_one_dict_for_query
            self.input_params_for_body |= param_one_dict_for_body

        # RAG参数校验
        threshold = self.input_params_for_body.get('threshold', None)
        if not (0 <= threshold <= 1):
            raise Exception("过滤阈值参数错误，应该在[0,1]之间")
        top_k = self.input_params_for_body.get('top_k', None)
        if not (0 <= top_k <= 10):
            raise Exception("选取知识条数参数错误，应该在[0,10]之间")
        question = self.input_params_for_body.get('question', None)
        if not question:
            raise Exception("RAG节点问题为空,请填写问题")
        if 'headers' not in params_dict['settings']:
            raise Exception("headers key not found in settings")
        if len(self.knowledgeBase) == 0:
            raise Exception('知识库参数为空，至少选择一个知识库')
        # 2. 使用 api_request 函数进行 API 请求设置
        self.input_params_for_body['knowledgeBase'] = self.knowledgeBase
        self.input_params_for_body['userId'] = self.userId

        # 适配知识库接口
        if 'top_k' in self.input_params_for_body:
            self.input_params_for_body['topK'] = self.input_params_for_body.pop('top_k')

        response = api_request_for_big_model(url=self.api_url, method='POST', headers=self.api_header,
                                             data=self.input_params_for_body)
        logger.info(f'RAG service response {response.content}')
        if response.status == service_status.ServiceExecStatus.ERROR:
            raise Exception(str(response.content))
        if response.content is None:
            raise Exception("Can not get response from RAG service")
        if len(response.content['data']['searchList']) == 0 and response.content['code'] != 0:
            raise Exception(f"{response.content['message']}")
        if len(response.content['data']['searchList']) == 0 and response.content['code'] == 0:
            raise Exception(f"无法在知识库匹配到结果，请调整参数设置")
        # 3. 拆包解析
        self.output_params = RAGNode.convert_rag_response(response.content)
        # 检查返回值是否对应
        for k in self.output_params_spec:
            if k not in self.output_params:
                logger.error(f"api response: {self.output_params}")
                raise Exception(f"user defined output parameter {k} not found in api response")

        self.dag_obj.update_params_pool_with_running_node(self.node_id, self.output_params)
        logger.info(
            f"{self.var_name}, run success, input params: {self.input_params}, output params: {self.output_params}")
        return self.output_params

    @staticmethod
    def convert_rag_request(origin_params_dict: dict) -> dict:
        # 将name为query替换为RAG识别的question
        for item in origin_params_dict['inputs']:
            if item['name'] == 'query':
                item['name'] = 'question'
        # 添加知识库url地址
        origin_params_dict['settings']['url'] = RAG_URL
        return origin_params_dict

    @staticmethod
    def convert_rag_response(origin_params_dict: dict) -> dict:
        prompt = origin_params_dict['data']['prompt']
        content = origin_params_dict['data']['searchList']
        updated_params_dict = {'prompt': prompt, 'content': content}
        return updated_params_dict


NODE_NAME_MAPPING = {
    "dashscope_chat": ModelNode,
    "openai_chat": ModelNode,
    "post_api_chat": ModelNode,
    "post_api_dall_e": ModelNode,
    "Message": MsgNode,
    "DialogAgent": DialogAgentNode,
    "UserAgent": UserAgentNode,
    "TextToImageAgent": TextToImageAgentNode,
    "DictDialogAgent": DictDialogAgentNode,
    "ReActAgent": ReActAgentNode,
    "Placeholder": PlaceHolderNode,
    "MsgHub": MsgHubNode,
    "SequentialPipeline": SequentialPipelineNode,
    "ForLoopPipeline": ForLoopPipelineNode,
    "WhileLoopPipeline": WhileLoopPipelineNode,
    "IfElsePipeline": IfElsePipelineNode,
    "SwitchPipeline": SwitchPipelineNode,
    "CopyNode": CopyNode,
    "BingSearchService": BingSearchServiceNode,
    "GoogleSearchService": GoogleSearchServiceNode,
    "PythonService": PythonServiceNode,
    "ReadTextService": ReadTextServiceNode,
    "WriteTextService": WriteTextServiceNode,

    # 自定义节点
    "StartNode": StartNode,
    "EndNode": EndNode,
    "PythonNode": PythonServiceUserTypingNode,
    "ApiNode": ApiNode,
    "LLMNode": LLMNode,
    "SwitchNode": SwitchNode,
    "RAGNode": RAGNode,
}


def get_all_agents(
        node: WorkflowNode,
        seen_agents: Optional[set] = None,
        return_var: bool = False,
) -> List:
    """
    Retrieve all unique agent objects from a pipeline.

    Recursively traverses the pipeline to collect all distinct agent-based
    participants. Prevents duplication by tracking already seen agents.

    Args:
        node (WorkflowNode): The WorkflowNode from which to extract agents.
        seen_agents (set, optional): A set of agents that have already been
            seen to avoid duplication. Defaults to None.

    Returns:
        list: A list of unique agent objects found in the pipeline.
    """
    if seen_agents is None:
        seen_agents = set()

    all_agents = []

    for participant in node.pipeline.participants:
        if participant.node_type == WorkflowNodeType.AGENT:
            if participant not in seen_agents:
                if return_var:
                    all_agents.append(participant.var_name)
                else:
                    all_agents.append(participant.pipeline)
                seen_agents.add(participant.pipeline)
        elif participant.node_type == WorkflowNodeType.PIPELINE:
            nested_agents = get_all_agents(
                participant,
                seen_agents,
                return_var=return_var,
            )
            all_agents.extend(nested_agents)
        else:
            raise TypeError(type(participant))

    return all_agents
