import uuid
import json

class WorkflowStatus:  # type: ignore[name-defined]
    WORKFLOW_PUBLISHED = "published",  # 已发布状态
    WORKFLOW_DRAFT = "draft"  # 未发布状态


def workflow_format_convert(origin_dict: dict) -> dict:
    converted_dict = {}
    nodes = origin_dict.get("nodes", [])
    edges = origin_dict.get("edges", [])

    for node in nodes:
        if len(node) == 0:
            raise Exception(f"异常: 前端透传了空节点，请确认")

        node_id = node["id"]
        node_data = {
            "data": {
                "args": node["data"]
            },
            "inputs": {
                "input_1": {
                    "connections": []
                }
            },
            "outputs": {
                "output_1": {
                    "connections": []
                }
            },
            "name": node["type"]
        }
        converted_dict.setdefault(node_id, node_data)

        for edge in edges:
            if edge["source_node_id"] == node_id:
                converted_dict[node_id]["outputs"]["output_1"]["connections"].append(
                    {'node': edge["target_node_id"], 'output': "input_1"}
                )
            elif edge["target_node_id"] == node_id:
                converted_dict[node_id]["inputs"]["input_1"]["connections"].append(
                    {'node': edge["source_node_id"], 'input': "output_1"}
                )

    return converted_dict


def node_format_convert(node_dict: dict) -> dict:
    converted_dict = {}
    node_id = node_dict["id"]
    node_data = {
        "data": {
            "args": node_dict["data"]
        },
        "inputs": {
            "input_1": {
                "connections": []
            }
        },
        "outputs": {
            "output_1": {
                "connections": []
            }
        },
        "name": node_dict["type"]
    }
    converted_dict.setdefault(node_id, node_data)
    return converted_dict


def get_workflow_running_result(nodes_result: list, execute_id: str, execute_status: str, execute_cost: str) -> dict:
    execute_result = {
        "execute_id": execute_id,
        "execute_status": execute_status,
        "execute_cost": execute_cost,
        "node_result": nodes_result
    }
    return execute_result


def standardize_single_node_format(data: dict) -> dict:
    for value in data.values():
        for field in ['inputs', 'outputs']:
            # 如果字段是一个字典，且'connections'键存在于字典中
            if 'input_1' in value[field]:
                # 将'connections'字典设置为[]
                value[field]['input_1']['connections'] = []
            elif 'output_1' in value[field]:
                value[field]['output_1']['connections'] = []
    return data


def plugin_desc_config_generator(data: dict) -> dict:
    dag_name = data['pluginName']
    dag_en_name = data['pluginENName']
    if dag_en_name == '':
        raise Exception("plugin english name not found")
    dag_desc = data['pluginDesc']
    dag_desc_example = data['pluginDescription']
    dag_spec_dict = data['pluginSpec']
    if not isinstance(dag_spec_dict, dict) or "nodes" not in dag_spec_dict:
        raise ValueError("Invalid workflow schema format")

    openapi_schema_dict = {"openapi": "3.0.0", "info": {
        "title": f"{dag_en_name} API",
        "version": "1.0.0",
        "description": f"{dag_desc}"
    }, "servers": [
        {
            "url": "http://106.74.31.170:5003/plugin/api"
        }
    ], "paths": {
        f"/run_for_bigmodel/{dag_en_name}": {
            "post": {
                "summary": f"{dag_name}, {dag_en_name}",
                "operationId": f"action_{dag_en_name}",
                "description": f"{dag_desc}, {dag_desc_example}",
                "parameters": [
                    {
                        "in": "header",
                        "name": "content-type",
                        "schema": {
                            "type": "string",
                            "example": "application/json"
                        },
                        "required": True
                    }
                ],
                "responses": {
                    "200": {
                        "description": "成功获取查询结果",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object"
                                }
                            }
                        }
                    },
                    "default": {
                        "description": "请求失败时的错误信息",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object"
                                }
                            }
                        }
                    }
                },
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": [],
                                "properties": {}
                            }
                        }
                    }
                }
            }
        }
    }}

    # 完善入参列表
    request_schema = openapi_schema_dict["paths"][f"/run_for_bigmodel/{dag_en_name}"]["post"]["requestBody"][
        "content"]["application/json"]["schema"]

    start_node_dict = {}
    for node in dag_spec_dict['nodes']:
        if "type" not in node:
            raise Exception("Invalid node schema")
        if node["type"] == "StartNode":
            start_node_dict = node
            break
    if len(start_node_dict) == 0:
        raise Exception("start node not found")

    for param in start_node_dict['data']['outputs']:
        param_name = param['name']
        param_type = param['type']
        param_desc = param['desc']

        # 简单起见，这里只考虑普通变量，不考虑嵌套类型变量，例如object和array
        request_schema['required'].append(param_name)
        request_schema['properties'][param_name] = {
            "type": param_type,
            "description": param_desc
        }

    return openapi_schema_dict


def gennerate_workflow_schema_template() -> str:
    start_node_id = str(uuid.uuid4())
    end_node_id = str(uuid.uuid4())
    workflow_schema = {
        "edges": [
            {
                "source_node_id": start_node_id,
                "target_node_id": end_node_id
            }
        ],
        "nodes": [
            {
                "data": {
                    "inputs": [],
                    "outputs": [
                        {
                            "name": "",
                            "type": "string",
                            "desc": "",
                            "object_schema": None,
                            "list_schema": None,
                            "value": {
                                "type": "generated",
                                "content": None
                            }
                        }
                    ],
                    "settings": {}
                },
                "id": start_node_id,
                "name": "开始",
                "type": "StartNode"
            },
            {
                "data": {
                    "inputs": [
                        {
                            "name": "",
                            "type": "string",
                            "desc": "",
                            "object_schema": None,
                            "list_schema": None,
                            "value": {
                                "type": "ref",
                                "content": {
                                    "ref_node_id": "",
                                    "ref_var_name": ""
                                }
                            }
                        }
                    ],
                    "outputs": [],
                    "settings": {}
                },
                "id": end_node_id,
                "name": "结束",
                "type": "EndNode"
            }
        ]
    }
    workflow_schema_json = json.dumps(workflow_schema)
    return workflow_schema_json
