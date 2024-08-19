# -*- coding: utf-8 -*-
"""Workflow node opt."""
from abc import ABC, abstractmethod
from enum import IntEnum
from typing import List, Optional
from loguru import logger

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
    ServiceToolkit,
    api_request,
)


import json

DEFAULT_FLOW_VAR = "flow"

params_pool = {}

def parse_json_to_dict(extract_text: str) -> dict:
    # Parse the content into JSON object
    try:
        parsed_json = json.loads(extract_text, strict=False)
        if not isinstance(parsed_json, dict):
            raise Exception(f"json text type({type(parsed_json)}) is not dict")
        return parsed_json
    except json.decoder.JSONDecodeError as e:
        raise e

def generate_python_param(param_spec: dict, params_pool: dict) -> dict:
    paramName = param_spec['name']

    if param_spec['value']['type'] == 'ref':
        referenceNodeName = param_spec['value']['content']['ref_node_id']
        referenceParamName = param_spec['value']['content']['ref_var_name']
        paramValue = params_pool[referenceNodeName][referenceParamName]
        return {paramName: paramValue}

    return {paramName: param_spec['value']['content']}


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
# 开始节点实际上是定义全局变量的，此外别无操作，那么可以参考massage_hub节点实现
class StartNode(WorkflowNode):
    """
<<<<<<< Updated upstream
    新增的开始节点用于接收和存储用户输入的变量为全局变量.
    opt_kwargs输入dict
    比如用户定义了该节点的输入和输出变量名、类型、value
=======
    开始节点代表输入参数列表.
    source_kwargs字段，用户定义了该节点的输入和输出变量名、类型、value
>>>>>>> Stashed changes

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
            params_json_str: str
    ) -> None:
        super().__init__(node_id, {}, {}, [])
        # source_kwargs 包含用户定义的节点输入输出变量
        # 开始节点没有输入，只有输出
        self.params_json_str = params_json_str

    def __call__(self, *args, **kwargs):
        return self.output_data

    def compile(self) -> dict:
        # 入参的在这里初始化
        # {
        #     "inputs": [...],
        #     "outputs": [...],
        #     "settings": {...}
        # }
        global params_pool
        params_pool.setdefault(self.node_id, {})

        logger.info(f"node type:{self.node_id}, name:{self.node_id}, start param parser")
        params_dict = parse_json_to_dict(self.params_json_str)
        logger.info(f"node type:{self.node_id}, name:{self.node_id}, params: {params_dict}")

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

        for i, param_spec in enumerate(params_dict['outputs']):
            # param_obj_dict 举例
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
            param_one_dict = generate_python_param(param_spec, params_pool)
            params_pool[self.node_id] |= param_one_dict

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
                 ) -> None:
        super().__init__(node_id, opt_kwargs, source_kwargs, dep_opts)
        # source_kwargs contains the list of variable names to retrieve
        self.inputs = opt_kwargs.get("inputs", [])
        self.variable_names = [input_data["name"] for input_data in self.inputs]
        self.input_data = {}

    def __call__(self, x: dict = None) -> dict:
        result = {}
        for var_name in self.variable_names:
            if var_name in globals():
                result[var_name] = globals()[var_name]
            else:
                result[var_name] = None
        print("EndNode Output:", result)

        return result

    def compile(self) -> dict:
        # 生成执行的代码，并逐行输出每个变量的查找结果
        inits = "\n".join(
            [
                f'print("Value for {var_name}: ", globals().get("{var_name}", None))'
                for var_name in self.variable_names
            ]
        )

        # 将找到的值存入 DEFAULT_FLOW_VAR 中
        inits += "\n" + "\n".join(
            [
                f'{DEFAULT_FLOW_VAR}["{var_name}"] = globals().get("{var_name}", None)'
                for var_name in self.variable_names
            ]
        )

        return {
            "imports": "",
            "inits": inits,
            "execs": "",
        }



class PythonServiceUserTypingNode(WorkflowNode):
    """
    Execute Python Node,支持用户输入
    使用 source_kwargs 获取用户输入的 Python 代码
    这个代码将作为 execute_python_code 函数的 code 参数传入。
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
        self.service_func = ServiceToolkit.get(execute_python_code)
        self.python_code = opt_kwargs.get("python_code", "")
        self.timeout = opt_kwargs.get("timeout", 300)
        self.use_docker = opt_kwargs.get("use_docker", None)
        self.maximum_memory_bytes = opt_kwargs.get("maximum_memory_bytes", None)
        # 添加输出参数名称(可选)
        self.result_var_name = opt_kwargs.get("result_var_name", "result")

    def compile(self) -> dict:
        # 将代码执行的所有必要参数格式化为字典
        execs = (
            f'{DEFAULT_FLOW_VAR} = {self.var_name}('
            f'code={repr(self.python_code)}, '
            f'timeout={self.timeout}, '
            f'use_docker={self.use_docker}, '
            f'maximum_memory_bytes={self.maximum_memory_bytes})'
            # 节点运行结果存入输出变量
            f'global_vars["{self.result_var_name}"] = {DEFAULT_FLOW_VAR}.result'
        )

        save_result = f'global_vars["{self.result_var_name}"] = {DEFAULT_FLOW_VAR}'
        return {
            "imports": "from agentscope.service import ServiceFactory\n"
                       "from agentscope.service import execute_python_code",
            "inits": f"{self.var_name} = ServiceFactory.get(execute_python_code)",
            "execs": execs,

        }


# 新增通用api调用节点，目前还不能使用需要输入auth key的api
class ApiServiceNode(WorkflowNode):
    """
    API Service Node for executing HTTP requests using the api_request function.
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
        # 将 api_request 函数与 opt_kwargs 参数一起传递给 ServiceToolkit
        self.service_func = ServiceToolkit.get(
            api_request,
            **self.opt_kwargs,
        )

    def compile(self) -> dict:
        return {
            "imports": "",
            "inits": "",
            "execs": "",
        }

    def __call__(self, *args, **kwargs):
        # 执行服务函数并返回结果
        return self.service_func


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
    "StartNode": StartNode,
    "EndNode": EndNode,
    "PythonServiceUserTypingNode": PythonServiceUserTypingNode,
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
