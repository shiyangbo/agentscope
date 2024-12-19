import json
import time

import agentscope.aibigmodel_workflow.utils as utils
import agentscope.utils.jwt_auth as auth

from datetime import datetime
from flask import jsonify
from sqlalchemy.exc import SQLAlchemyError
from loguru import logger
from agentscope.web.workstation.workflow_utils import WorkflowNodeStatus
from agentscope.utils.jwt_auth import SIMPLE_CLOUD, PRIVATE_CLOUD
from agentscope.web.workstation.workflow_dag import build_dag
from config import db, SERVICE_URL
from database import _ExecuteTable, _WorkflowTable, _PluginTable


def plugin_publish(workflow_id, user_id, workflow_result, plugin_field, description):
    # 插件描述信息生成，对接智能体格式
    dag_content = json.loads(workflow_result.dag_content)

    data = {
        "pluginName": workflow_result.config_name,
        "pluginDesc": workflow_result.config_desc,
        "pluginENName": workflow_result.config_en_name,
        "pluginField": plugin_field,
        "pluginDescription": description,
        "pluginSpec": dag_content,
        "identifier": user_id if auth.get_cloud_type() == SIMPLE_CLOUD else workflow_result.tenant_id,
        "serviceURL": SERVICE_URL
    }

    try:
        openapi_schema = utils.plugin_desc_config_generator(data)
        openapi_schema_json_str = json.dumps(openapi_schema)

        db.session.add(
            _PluginTable(
                id=workflow_id,
                user_id=user_id,
                plugin_name=workflow_result.config_name,
                plugin_en_name=workflow_result.config_en_name,
                plugin_desc=workflow_result.config_desc,
                dag_content=workflow_result.dag_content,
                plugin_field=data["pluginField"],
                plugin_desc_config=openapi_schema_json_str,
                published_time=datetime.now(),
                tenant_id=workflow_result.tenant_id
            ),
        )
        db.session.query(_WorkflowTable).filter_by(id=workflow_id).update(
            {_WorkflowTable.status: utils.WorkflowStatus.WORKFLOW_PUBLISHED})
        db.session.commit()
    except SQLAlchemyError as e:
        db.session.rollback()
        return jsonify({"code": 7, "msg": str(e)})
    except Exception as e:
        logger.error(f"plugin_publish failed: {e}")
        return jsonify({"code": 7, "msg": str(e)})

    return jsonify({"code": 0, "msg": "Workflow file published successfully"})


def build_and_run_dag(workflow_schema, content):
    try:
        converted_config = utils.workflow_format_convert(workflow_schema)
        logger.info(f"config: {converted_config}")
        dag = build_dag(converted_config)
    except Exception as e:
        logger.error(f"Workflow_run failed: {repr(e)}")
        return None, None, None, WorkflowNodeStatus.FAILED

    start_time = time.time()
    try:
        result, nodes_result = dag.run_with_param(content, workflow_schema)
    except Exception as e:
        logger.error(f"Workflow execution failed: {repr(e)}")
        return dag, None, None, WorkflowNodeStatus.FAILED

    end_time = time.time()
    executed_time = round(end_time - start_time, 3)
    execute_status = WorkflowNodeStatus.SUCCESS if all(
        node.get('node_status') in [WorkflowNodeStatus.SUCCESS, WorkflowNodeStatus.RUNNING_SKIP]
        for node in nodes_result) else WorkflowNodeStatus.FAILED
    execute_result = utils.get_workflow_running_result(nodes_result, dag.uuid, execute_status, str(executed_time))

    logger.info(f"Execute_result: {execute_result}")
    execute_result = json.dumps(execute_result)

    return dag, result, execute_result, execute_status


def workflow_run(workflow_id, workflow_result, workflow_schema, content):
    user_id = auth.get_user_id()
    tenant_ids = auth.get_tenant_ids()
    cloud_type = auth.get_cloud_type()

    # 生成执行插件dag
    dag, result, execute_result, execute_status = build_and_run_dag(workflow_schema, content)
    if not execute_result:
        return jsonify({"code": 7, "msg": "execute result not exists"})

    try:
        # 限制一个用户执行记录为500次，超过500次删除最旧的记录
        query = db.session.query(_ExecuteTable).filter_by(user_id=user_id if user_id == 'SIMPLE_CLOUD' else
        _ExecuteTable.tenant_id.in_(tenant_ids))

        if query.count() > 500:
            oldest_record = db.session.query(_ExecuteTable).filter_by(user_id=user_id).order_by(
                _ExecuteTable.executed_time).first() if cloud_type == SIMPLE_CLOUD else db.session.query(
                _ExecuteTable).filter(_ExecuteTable.tenant_id.in_(tenant_ids)).order_by(
                _ExecuteTable.executed_time).first()
            if oldest_record:
                db.session.delete(oldest_record)

        db.session.add(_ExecuteTable(execute_id=dag.uuid, execute_result=execute_result, user_id=user_id,
                                     executed_time=datetime.now(), workflow_id=workflow_id,
                                     tenant_id=workflow_result.tenant_id))

        if cloud_type == SIMPLE_CLOUD:
            db.session.query(_WorkflowTable).filter_by(id=workflow_id, user_id=user_id).update({
                _WorkflowTable.execute_status: execute_status})
        elif cloud_type == PRIVATE_CLOUD:
            db.session.query(_WorkflowTable).filter(
                _WorkflowTable.id == workflow_id, _WorkflowTable.tenant_id.in_(tenant_ids)).update(
                {_WorkflowTable.execute_status: execute_status})
        else:
            return jsonify({"code": 7, "msg": "不支持的云类型"})
        db.session.commit()
    except SQLAlchemyError as e:
        db.session.rollback()
        return jsonify({"code": 7, "msg": str(e)})

    return jsonify(code=0, data=result, executeID=dag.uuid)


def plugin_run_for_bigmodel(plugin, input_params, plugin_en_name):
    try:
        # 存入数据库的数据为前端格式，需要转换为后端可识别格式
        config = json.loads(plugin.dag_content)
        converted_config = utils.workflow_format_convert(config)
        dag = build_dag(converted_config)
    except Exception as e:
        logger.error(f"plugin_run_for_bigmodel failed: {repr(e)}")
        return json.dumps({"code": 7, "msg": repr(e)})

    # 调用运行dag
    start_time = time.time()
    result, nodes_result = dag.run_with_param(input_params, config)
    # 检查是否如期运行
    for node_dict in nodes_result:
        node_status = node_dict['node_status']
        if node_status == WorkflowNodeStatus.FAILED:
            node_message = node_dict['node_message']
            return json.dumps({"code": 7, "msg": node_message})

    end_time = time.time()
    executed_time = round(end_time - start_time, 3)
    # 获取workflow与各节点的执行结果
    execute_status = WorkflowNodeStatus.SUCCESS if all(
        node.get('node_status') in [WorkflowNodeStatus.SUCCESS, WorkflowNodeStatus.RUNNING_SKIP]
        for node in nodes_result) else WorkflowNodeStatus.FAILED
    execute_result = utils.get_workflow_running_result(nodes_result, dag.uuid, execute_status, str(executed_time))
    if not execute_result:
        return json.dumps({"code": 7, "msg": "execute result not exists"})

    # 大模型调用时，不需要增加数据库流水记录
    logger.info(f"=== AI request: {plugin_en_name=}, result: {result}, execute_result: {execute_result}")
    return json.dumps(result, ensure_ascii=False)


def get_workflow_list(cloud_type, keyword=None, status=None, page=1, limit=10):
    try:
        if cloud_type == SIMPLE_CLOUD:
            user_id = auth.get_user_id()
            if not user_id:
                return jsonify({"code": 7, "msg": "userID is required"})
            query = db.session.query(_WorkflowTable).filter_by(user_id=user_id)
        elif cloud_type == PRIVATE_CLOUD:
            tenant_ids = auth.get_tenant_ids()
            if len(tenant_ids) == 0:
                return jsonify({"code": 7, "msg": "tenant_ids is required"})
            query = db.session.query(_WorkflowTable).filter(
                _WorkflowTable.tenant_id.in_(tenant_ids)
            )
        else:
            return jsonify({"code": 7, "msg": "不支持的云类型"})

        if keyword:
            query = query.filter(_WorkflowTable.config_name.contains(keyword) |
                                 _WorkflowTable.config_en_name.contains(keyword))
        if status:
            query = query.filter_by(status=status)

        # 获取符合user_id条件的所有记录数
        total = query.count()

        # 分页查询
        workflows = query.order_by(_WorkflowTable.updated_time.desc()).paginate(page=int(page), per_page=int(limit))

        workflows_list = [workflow.to_dict() for workflow in workflows]
        data = {"list": workflows_list, "pageNo": int(page), "pageSize": int(limit), "total": total}
        return jsonify({"code": 0, "data": data})
    except SQLAlchemyError as e:
        logger.error(f"Error occurred while fetching workflow list: {e}")
        return jsonify({"code": 5000, "msg": "Error occurred while fetching workflow list."})
