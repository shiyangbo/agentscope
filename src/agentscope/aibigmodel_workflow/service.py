import json
import time

import agentscope.aibigmodel_workflow.utils as utils
import agentscope.utils.jwt_auth as auth

from datetime import datetime
from flask import jsonify
from sqlalchemy.exc import SQLAlchemyError
from loguru import logger
import database
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


def workflow_clone(workflow_config, user_id, tenant_ids):
    # 查询相同英文名称的工作流配置，并为新副本生成唯一的名称
    if auth.get_cloud_type() == SIMPLE_CLOUD:
        existing_config_copies = database.fetch_records_by_filters(_WorkflowTable,
                                                                   method='all',
                                                                   config_en_name=workflow_config.config_en_name,
                                                                   user_id=user_id)
    elif auth.get_cloud_type() == PRIVATE_CLOUD:
        existing_config_copies = database.fetch_records_by_filters(_WorkflowTable,
                                                                   method='all',
                                                                   config_en_name=workflow_config.config_en_name,
                                                                   tenant_id__in=tenant_ids)
    else:
        return jsonify({"code": 7, "msg": "不支持的云类型"})
    # 找出最大后缀
    name_suffix = utils.add_max_suffix(workflow_config.config_en_name, existing_config_copies)
    # 生成新的配置名称和状态
    new_config_name = f"{workflow_config.config_name}_副本{name_suffix}"
    new_config_en_name = f"{workflow_config.config_en_name}_{name_suffix}"
    try:
        # 生成新的工作流 ID
        new_workflow_id = uuid.uuid4()
        # 创建新工作流记录
        new_workflow = _WorkflowTable(
            id=str(new_workflow_id),
            user_id=workflow_config.user_id,
            config_name=new_config_name,
            config_en_name=new_config_en_name,
            config_desc=workflow_config.config_desc,
            dag_content=workflow_config.dag_content,
            status=utils.WorkflowStatus.WORKFLOW_DRAFT,
            updated_time=datetime.now(),
            tenant_id=workflow_config.tenant_id
        )
        db.session.add(new_workflow)
        db.session.commit()
    except SQLAlchemyError as e:
        db.session.rollback()
        return jsonify({"code": 5000, "message": str(e)})
    except Exception as e:
        db.session.rollback()
        logger.error(f"workflow_clone failed: {e}")
        return jsonify({"code": 7, "message": str(e)})

    # 返回新创建的工作流信息
    response_data = {
        "code": 0,
        "data": {"workflow_id": new_workflow.id},
        "msg": "Workflow cloned successfully"
    }
    return jsonify(response_data)


def workflow_save(workflow_id, config_name, config_en_name, config_desc, workflow_dict, user_id, tenant_ids):
    # 查询条件
    if auth.get_cloud_type() == SIMPLE_CLOUD:
        workflow_results = database.fetch_records_by_filters(_WorkflowTable,
                                                             id=workflow_id,
                                                             user_id=user_id)
    elif auth.get_cloud_type() == PRIVATE_CLOUD:
        workflow_results = database.fetch_records_by_filters(_WorkflowTable,
                                                             id=workflow_id,
                                                             tenant_id__in=tenant_ids)
    else:
        return jsonify({"code": 7, "msg": "不支持的云类型"})

    if not workflow_results:
        return jsonify({"code": 5000, "msg": "Internal Server Error"})

    if auth.get_cloud_type() == SIMPLE_CLOUD:
        # 检查英文名称唯一性
        en_name_check = database.fetch_records_by_filters(_WorkflowTable,
                                                          config_en_name=config_en_name,
                                                          user_id=user_id)
    elif auth.get_cloud_type() == PRIVATE_CLOUD:
        en_name_check = database.fetch_records_by_filters(_WorkflowTable,
                                                          config_en_name=config_en_name,
                                                          tenant_id__in=tenant_ids)
    else:
        return jsonify({"code": 7, "msg": "不支持的云类型"})
    if en_name_check and config_en_name != workflow_results.config_en_name:
        return jsonify({"code": 7, "msg": "该英文名称已存在, 请重新填写"})

    try:
        workflow = json.dumps(workflow_dict)
        # 防御性措施
        if len(workflow_dict['nodes']) == 0 or workflow_dict['nodes'] == [{}]:
            workflow = utils.generate_workflow_schema_template()

        # 更新逻辑
        update_data = {
            _WorkflowTable.config_name: config_name,
            _WorkflowTable.config_en_name: config_en_name,
            _WorkflowTable.config_desc: config_desc,
            _WorkflowTable.dag_content: workflow,
            _WorkflowTable.updated_time: datetime.now(),
            _WorkflowTable.execute_status: ""
        }

        if cloud_type == SIMPLE_CLOUD:
            db.session.query(_WorkflowTable).filter_by(id=workflow_id, user_id=user_id).update(update_data)
        else:
            db.session.query(_WorkflowTable).filter(
                _WorkflowTable.id == workflow_id,
                _WorkflowTable.tenant_id.in_(tenant_ids)
            ).update(update_data)

        db.session.commit()
    except SQLAlchemyError as e:
        db.session.rollback()
        return jsonify({"code": 5000, "msg": str(e)})

    return jsonify({"code": 0, "data": {"workflowID": str(workflow_id)}, "msg": "Workflow file saved successfully"})

def workflow_create(workflow_config, user_id, tenant_ids):

