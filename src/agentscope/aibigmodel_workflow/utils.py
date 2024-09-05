from app import SERVICE_URL


class WorkflowStatus:  # type: ignore[name-defined]
    WORKFLOW_PUBLISHED = "published",  # 已发布状态
    WORKFLOW_DRAFT = "draft"  # 未发布状态


def workflow_format_convert(origin_dict: dict) -> dict:
    converted_dict = {}
    nodes = origin_dict.get("nodes", [])
    edges = origin_dict.get("edges", [])

    for node in nodes:
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
    plugin_desc_config = {
        "name_for_human": data["pluginName"],
        "name_for_model": data["pluginENName"],
        "desc_for_human": data["pluginDesc"],
        "desc_for_model": data["pluginDesc"],
        "field": data["pluginField"],
        "question_example": data["pluginQuestionExample"],
        "answer_example": data["pluginENName"],
        "confirm_required": "false",
        "api_info": {
            "url": SERVICE_URL,  # 后续改为服务部署的url地址
            "method": "post",
            "content_type": "application/json",
            "input_params": [
                {
                    "name": "pluginName",
                    "description": "插件名称",
                    "required": "true",
                    "schema": {
                        "type": "string",
                        "default": "插件名称"
                    },
                    "para_example": "插件名称"
                },
                {
                    "name": "data",
                    "description": "问题",
                    "required": "true",
                    "schema": {
                        "type": "string",
                        "default": data["pluginQuestionExample"]
                    },
                    "para_example": data["pluginQuestionExample"]
                }
            ]
        },
        "version": "1.0",
        "contact_email": "test@163.com"
    }
    return plugin_desc_config
