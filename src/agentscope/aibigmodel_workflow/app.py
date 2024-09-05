# -*- coding: utf-8 -*-
"""The Web Server of the AgentScope Studio."""
import json
import os
import re
import subprocess
import time
import tempfile
import threading
import traceback
import sys
from datetime import datetime

sys.path.append('/agentscope/agentscope/src')

from typing import Tuple, Union, Optional

from sqlalchemy.exc import SQLAlchemyError

from agentscope.web.workstation.workflow_dag import build_dag
from agentscope._runtime import _runtime
from flask import (
    Flask,
    request,
    jsonify,
    Response,
    abort,
)
from loguru import logger
import sqlite3
import uuid
import yaml

from flask import Flask
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO

import agentscope.aibigmodel_workflow.utils as utils

app = Flask(__name__)

# 读取 YAML 文件
test_without_mysql = False
if test_without_mysql:
    # Set the cache directory
    from pathlib import Path

    _cache_dir = Path.home() / ".cache" / "agentscope-studio"
    _cache_db = _cache_dir / "agentscope.db"
    os.makedirs(str(_cache_dir), exist_ok=True)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{str(_cache_db)}"
else:
    with open('/agentscope/agentscope/src/agentscope/aibigmodel_workflow/sql_config.yaml', 'r') as file:
        config = yaml.safe_load(file)

    # 从 YAML 文件中提取参数
    DIALECT = config['DIALECT']
    DRIVER = config['DRIVER']
    USERNAME = config['USERNAME']
    PASSWORD = config['PASSWORD']
    HOST = config['HOST']
    PORT = config['PORT']
    DATABASE = config['DATABASE']
    SERVICE_URL = config['SERVICE_URL']

    SQLALCHEMY_DATABASE_URI = "{}+{}://{}:{}@{}:{}/{}?charset=utf8".format(DIALECT, DRIVER, USERNAME, PASSWORD, HOST,
                                                                           PORT,
                                                                           DATABASE)
    print(SQLALCHEMY_DATABASE_URI)
    app.config['SQLALCHEMY_DATABASE_URI'] = SQLALCHEMY_DATABASE_URI
    app.config['SQLALCHEMY_ECHO'] = True
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        'pool_size': 5,
        'pool_timeout': 30,
        'pool_recycle': -1,
        'pool_pre_ping': True
    }

db = SQLAlchemy()
db.init_app(app)

socketio = SocketIO(app)

# This will enable CORS for all route
CORS(app)

_RUNS_DIRS = []


class _ExecuteTable(db.Model):  # type: ignore[name-defined]
    """Execute workflow."""
    __tablename__ = "llm_execute_info"
    execute_id = db.Column(db.String(100), primary_key=True)  # 运行ID
    execute_result = db.Column(db.Text)
    user_id = db.Column(db.String(100))  # 用户ID


class _WorkflowTable(db.Model):  # type: ignore[name-defined]
    """Workflow store table."""
    __tablename__ = "llm_workflow_info"
    id = db.Column(db.String(100), primary_key=True)  # workflowID
    user_id = db.Column(db.String(100))  # 用户ID
    config_name = db.Column(db.String(100))
    config_en_name = db.Column(db.String(100), unique=True)
    config_desc = db.Column(db.Text)
    dag_content = db.Column(db.Text)
    status = db.Column(db.String(10))
    updated_time = db.Column(db.DateTime)

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'config_name': self.config_name,
            'config_en_name': self.config_en_name,
            'config_desc': self.config_desc,
            'status': self.status,
            'updated_time': self.updated_time
        }


class _PluginTable(db.Model):  # type: ignore[name-defined]
    """Plugin table."""
    __tablename__ = "llm_plugin_info"
    id = db.Column(db.String(100), primary_key=True)  # ID
    user_id = db.Column(db.String(100))  # 用户ID
    plugin_name = db.Column(db.String(100))  # 插件名称
    plugin_en_name = db.Column(db.String(100))  # 插件英文名称
    plugin_desc = db.Column(db.Text)  # 插件描述
    dag_content = db.Column(db.Text)  # 插件dag配置文件
    plugin_field = db.Column(db.String(100))  # 插件领域
    plugin_desc_config = db.Column(db.Text)  # 插件描述配置文件
    published_time = db.Column(db.DateTime)  # 插件发布时间


# 发布调试成功的workflow
@app.route("/plugin/api/publish", methods=["POST"])
def plugin_publish() -> Response:
    workflow_id = request.json.get("workflowID")
    summary = request.json.get("pluginField")
    description = request.json.get("pluginQuestionExample")
    user_id = request.headers.get("X-User-Id")
    # 查询workflow_info表获取插件信息
    workflow_result = _WorkflowTable.query.filter(
        _WorkflowTable.id == workflow_id
    ).first()
    if not workflow_result:
        return jsonify({"code": 400, "message": "No workflow config data exists"})
    # 插件描述信息生成，对接智能体格式
    data = {
        "pluginName": workflow_result.config_name,
        "pluginDesc": workflow_result.config_desc,
        "pluginENName": workflow_result.config_en_name,
        "pluginSummary": summary,
        "pluginDescription": description
    }
    plugin_desc_config = utils.plugin_desc_config_generator(data)
    plugin_desc_config_json_str = json.dumps(plugin_desc_config)

    # 数据库存储
    plugin = _PluginTable.query.filter(
        _PluginTable.user_id == user_id,
        _PluginTable.plugin_en_name == data["pluginENName"],
    ).all()
    # 插件的英文名称唯一
    if len(plugin) > 0:
        return jsonify({"code": 400, "message": f"Multiple records found for plugin en name: {data['pluginENName']}"})

    try:
        db.session.add(
            _PluginTable(
                id=workflow_id,
                user_id=user_id,
                plugin_name=workflow_result.config_name,
                plugin_en_name=workflow_result.config_en_name,
                plugin_desc=workflow_result.config_desc,
                dag_content=workflow_result.dag_content,
                plugin_field=data["pluginField"],
                plugin_desc_config=plugin_desc_config_json_str,
                published_time=datetime.now()
            ),
        )
        db.session.query(_WorkflowTable).filter_by(id=workflow_id).update(
            {_WorkflowTable.status: utils.WorkflowStatus.WORKFLOW_PUBLISHED})
        db.session.commit()
    except SQLAlchemyError as e:
        db.session.rollback()
        raise
    # 调用agent智能体接口，将插件进行注册

    return jsonify({"code": 0, "message": "Workflow file published successfully"})


# 已经发布的workflow直接运行
@app.route("/plugin/api/run_for_bigmodel/<plugin_en_name>", methods=["POST"])
def plugin_run_for_bigmodel(plugin_en_name) -> Response:
    """
    Input query data and get response.
    """
    if plugin_en_name == "":
        return jsonify({"code": 400, "message": "plugin_en_name empty"})

    # 大模型的入参适配
    input_params = request.json
    if not isinstance(input_params, dict):
        return jsonify({"code": 400, "message": f"input param type is {type(input_params)}, not dict"})

    plugin = _PluginTable.query.filter_by(plugin_en_name=plugin_en_name).first()
    if not plugin:
        return jsonify({"code": 400, "message": "plugin not exists"})

    try:
        # 存入数据库的数据为前端格式，需要转换为后端可识别格式
        config = json.loads(plugin.dag_content)
        converted_config = utils.workflow_format_convert(config)
        dag = build_dag(converted_config)
    except Exception as e:
        return jsonify({"code": 400, "message": repr(e)})

    # 调用运行dag
    start_time = time.time()
    result, nodes_result = dag.run_with_param(input_params, config)
    # 检查是否如期运行
    for node_dict in nodes_result:
        node_status = node_dict['node_status']
        if 'failed' in node_status:
            return jsonify({"code": 400, "message": node_status})

    end_time = time.time()
    executed_time = round(end_time - start_time, 3)
    # 获取workflow与各节点的执行结果
    execute_status = 'success' if all(node['node_status'] == 'success' for node in nodes_result) else 'failed'
    execute_result = utils.get_workflow_running_result(nodes_result, dag.uuid, execute_status, str(executed_time))
    if not execute_result:
        return jsonify({"code": 400, "message": "execute result not exists"})

    # 大模型调用时，不需要增加数据库流水记录
    logger.info(f"{plugin_en_name=}, execute_result: {execute_result}")
    return result


@app.route("/node/run", methods=["POST"])
def node_run() -> Response:
    """
    Input query data and get response.
    """
    # 用户输入的data信息，包含start节点所含信息，config文件存储地址
    content = {"content": "node_run"}
    nodes = request.json.get("nodeSchema")
    nodes_value = nodes.get("nodes", [])
    if len(nodes_value) != 1:
        message = "Node schema is invalid"
        return jsonify({"code": 400, "message": message})
    node = nodes_value[0]

    try:
        # 使用node_id, 获取需要运行的node配置
        node_config = utils.node_format_convert(node)
        dag = build_dag(node_config)
    except Exception as e:
        return jsonify({"code": 400, "message": repr(e)})

    # content中的data内容
    result, nodes_result = dag.run_with_param(content, nodes)
    if len(nodes_result) != 1:
        return jsonify({"code": 400, "message": nodes_result})
    if nodes_result[0]["node_status"] != 'success':
        return jsonify({"code": 400, "message": nodes_result[0]["node_status"]})

    return jsonify(code=0, result=nodes_result[0]["outputs"])


# 画布中的workflow，调试运行
@app.route("/workflow/run", methods=["POST"])
def workflow_run() -> Response:
    """
    Input query data and get response.
    """
    # 用户输入的data信息，包含start节点所含信息，config文件存储地址
    content = request.json.get("data")
    if not isinstance(content, dict):
        return jsonify({"code": 400, "message": f"input param type is {type(content)}, not dict"})

    workflow_schema = request.json.get("workflowSchema")
    user_id = request.headers.get("X-User-Id")
    logger.info(f"workflow_schema: {workflow_schema}")

    try:
        # 存入数据库的数据为前端格式，需要转换为后端可识别格式
        converted_config = utils.workflow_format_convert(workflow_schema)
        logger.info(f"config: {converted_config}")
        dag = build_dag(converted_config)
    except Exception as e:
        return jsonify({"code": 400, "message": repr(e)})

    start_time = time.time()
    result, nodes_result = dag.run_with_param(content, workflow_schema)
    end_time = time.time()
    executed_time = round(end_time - start_time, 3)
    # 获取workflow与各节点的执行结果
    execute_status = 'success' if all(node['node_status'] == 'success' for node in nodes_result) else 'failed'
    execute_result = utils.get_workflow_running_result(nodes_result, dag.uuid, execute_status, str(executed_time))
    # 需要持久化
    logger.info(f"execute_result: {execute_result}")
    execute_result = json.dumps(execute_result)
    if not execute_result:
        return jsonify({"code": 400, "message": "execute result not exists"})
    # 数据库存储
    try:
        db.session.add(
            _ExecuteTable(
                execute_id=dag.uuid,
                execute_result=execute_result,
                user_id=user_id
            ),
        )
        db.session.commit()
    except SQLAlchemyError as e:
        db.session.rollback()
        raise e

    return jsonify(code=0, result=result, executeID=dag.uuid)


@app.route("/workflow/create", methods=["POST"])
def workflow_create() -> Response:
    # request 参数获取
    data = request.json
    config_name = data.get("configName")
    config_en_name = data.get("configENName")
    config_desc = data.get("configDesc")
    if not config_name or not config_en_name or not config_desc:
        return jsonify({"code": 400, "message": "configName,configENName,configDesc is required"})
    user_id = request.headers.get("X-User-Id")
    # 查询表中同一用户下是否有重复config_en_name的记录
    workflow_results = _WorkflowTable.query.filter(
        _WorkflowTable.user_id == user_id,
        _WorkflowTable.config_en_name == config_en_name
    ).all()
    if not workflow_results:
        try:
            workflow_id = uuid.uuid4()
            db.session.add(
                _WorkflowTable(
                    id=str(workflow_id),
                    user_id=user_id,
                    config_name=config_name,
                    config_en_name=config_en_name,
                    config_desc=config_desc,
                    status=utils.WorkflowStatus.WORKFLOW_DRAFT,
                    updated_time=datetime.now()
                ),
            )
            db.session.commit()
        except SQLAlchemyError as e:
            db.session.rollback()
            raise e
        return jsonify({"code": 0, "workflowID": str(workflow_id), "message": "Workflow file created successfully"})
    else:
        return jsonify({"code": 400, "message": "该英文名称已存在, 请重新填写"})


@app.route("/workflow/delete", methods=["POST"])
def workflow_delete() -> Response:
    workflow_id = request.args.get("workflowID")
    user_id = request.headers.get("X-User-Id")
    workflow_results = _WorkflowTable.query.filter(
        _WorkflowTable.user_id == user_id,
        _WorkflowTable.id == workflow_id
    ).all()
    if workflow_results:
        try:
            db.session.query(_WorkflowTable).filter_by(id=workflow_id, user_id=user_id).delete()
            db.session.query(_PluginTable).filter_by(id=workflow_id, user_id=user_id).delete()
            db.session.commit()
        except SQLAlchemyError as e:
            db.session.rollback()
            return jsonify({"code": 500, "message": str(e)})
        return jsonify({"code": 0, "message": "Workflow file deleted successfully"})
    else:
        return jsonify({"code": 400, "message": "Record not found"})


@app.route("/workflow/save", methods=["POST"])
def workflow_save() -> Response:
    """
    Save the workflow JSON data to the local user folder.
    """
    # request 参数获取
    data = request.json
    config_name = data.get("configName")
    config_en_name = data.get("configENName")
    config_desc = data.get("configDesc")
    workflow_str = data.get("workflowSchema")
    workflow_id = data.get("workflowID")
    user_id = request.headers.get("X-User-Id")
    if not workflow_id or not user_id:
        return jsonify({"code": 400, "message": "workflowID and userID is required"})
    workflow_results = _WorkflowTable.query.filter(
        _WorkflowTable.user_id == user_id,
        _WorkflowTable.config_name == config_name,
        _WorkflowTable.id == workflow_id
    ).all()
    # 不存在记录则报错，存在则更新
    if workflow_results:
        # 查询数据库中是否有除这个workflow_id以外config_en_name相同的记录
        try:
            workflow = json.dumps(workflow_str)
            db.session.query(_WorkflowTable).filter_by(id=workflow_id, user_id=user_id).update(
                {_WorkflowTable.config_name: config_name,
                 _WorkflowTable.config_en_name: config_en_name,
                 _WorkflowTable.config_desc: config_desc,
                 _WorkflowTable.dag_content: workflow,
                 _WorkflowTable.updated_time: datetime.now()})
            db.session.commit()
        except SQLAlchemyError as e:
            db.session.rollback()
            return jsonify({"code": 500, "message": str(e)})
        return jsonify({"code": 0, "workflowID": workflow_id, "message": "Workflow file saved successfully"})
    else:
        return jsonify({"code": 500, "message": "Internal Server Error"})


@app.route("/workflow/clone", methods=["POST"])
def workflow_copy() -> Response:
    """
    Copy the workflow JSON data as a new one.
    """
    data = request.json
    workflow_id = data.get("workflowID")
    user_id = request.headers.get("X-User-Id")

    if not workflow_id:
        return jsonify({"code": 400, "message": "workflowID is required"})

    if not user_id:
        return jsonify({"code": 400, "message": "userID is required"})

    # 查找工作流配置
    workflow_config = _WorkflowTable.query.filter_by(id=workflow_id, user_id=user_id).first()
    if not workflow_config:
        return jsonify({"code": 400, "message": "workflow_config does not exist"})

    try:
        config_name = workflow_config.config_name
        config_en_name = workflow_config.config_en_name

        # 查询相同名称的工作流配置，并为新副本生成唯一的名称
        existing_config_copies = _WorkflowTable.query.filter(
            _WorkflowTable.config_name.like(f"{config_name}%"),
            _WorkflowTable.user_id == user_id
        ).all()

        # 计算新的名称后缀，找出最大后缀
        existing_suffixes = []
        for config_copy in existing_config_copies:
            match = re.match(rf"{re.escape(config_name)}_(\d+)",  config_copy.config_name)
            if match:
                existing_suffixes.append(int(match.group(1)))
        # 找出最大后缀
        name_suffix = max(existing_suffixes, default=0) + 1

        # 生成新的配置名称和状态
        new_config_name = f"{config_name}_{name_suffix}"
        new_config_en_name = f"{config_en_name}_{name_suffix}"
        new_status = utils.WorkflowStatus.WORKFLOW_DRAFT \
            if workflow_config.status == utils.WorkflowStatus.WORKFLOW_PUBLISHED else workflow_config.status

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
            status=new_status,
            updated_time=datetime.now()
        )
        db.session.add(new_workflow)
        db.session.commit()

        # 返回新创建的工作流信息
        response_data = {
            "code": 0,
            "message": "",
            "result": {
                "id": new_workflow.id,
                "configName": new_workflow.config_name,
                "configENName": new_workflow.config_en_name,
                "configDesc": new_workflow.config_desc,
                "status": new_workflow.status
            }
        }
        return jsonify(response_data)

    except SQLAlchemyError as e:
        db.session.rollback()
        return jsonify({"code": 500, "message": str(e)})


@app.route("/workflow/get", methods=["GET"])
def workflow_get() -> tuple[Response, int] | Response:
    """
    Reads and returns workflow data from the specified JSON file.
    """
    user_id = request.headers.get('X-User-Id')
    workflow_id = request.args.get('workflowID')
    if not workflow_id:
        return jsonify({"error": "workflowID is required"}), 400

    workflow_config = _WorkflowTable.query.filter_by(id=workflow_id, user_id=user_id).first()
    if not workflow_config:
        return jsonify({"code": 400, "message": "workflow_config not exists"})

    dag_content = json.loads(workflow_config.dag_content)
    return jsonify(
        {
            "code": 0,
            "result": dag_content,
            "message": ""
        },
    )


@app.route("/workflow/status", methods=["GET"])
def workflow_get_process() -> tuple[Response, int] | Response:
    """
    Reads and returns workflow process results from the specified JSON file.
    """
    execute_id = request.args.get("executeID")
    user_id = request.headers.get('X-User-Id')
    workflow_result = _ExecuteTable.query.filter_by(execute_id=execute_id, user_id=user_id).first()
    if not workflow_result:
        return jsonify({"code": 400, "message": "workflow_result not exists"})

    workflow_result.execute_result = json.loads(workflow_result.execute_result)
    return jsonify(
        {
            "code": 0,
            "result": workflow_result.execute_result,
            "message": ""
        },
    )


@app.route("/workflow/list", methods=["GET"])
def workflow_get_list() -> tuple[Response, int] | Response:
    """
    Reads and returns workflow data from the specified JSON file.
    """
    user_id = request.headers.get('X-User-Id')
    page = request.args.get('page', default=1)
    limit = request.args.get('limit', default=10)
    keyword = request.args.get('keyword', default='')
    status = request.args.get('status', default='')
    if not user_id:
        return jsonify({"code": 400, "message": "workflowID is required"})

    try:
        query = db.session.query(_WorkflowTable).filter_by(user_id=user_id)
        if keyword:
            query = query.filter(_WorkflowTable.config_name.contains(keyword) |
                                 _WorkflowTable.config_en_name.contains(keyword))
        if status:
            query = query.filter_by(status=status)

        # 分页查询
        workflows = query.paginate(page=int(page), per_page=int(limit))

        workflows_list = [workflow.to_dict() for workflow in workflows]

        return jsonify({"code": 0, "result": workflows_list})
    except SQLAlchemyError as e:
        app.logger.error(f"Error occurred while fetching workflow list: {e}")
        return jsonify({"code": 500, "message": "Error occurred while fetching workflow list."})


def init(
        host: str = "0.0.0.0",
        port: int = 6671,
        run_dirs: Optional[Union[str, list[str]]] = None,
        debug: bool = False,
) -> None:
    """Start the AgentScope Studio web UI with the given configurations.

    Args:
        host (str, optional):
            The host of the web UI. Defaults to "127.0.0.1"
        port (int, optional):
            The port of the web UI. Defaults to 5000.
        run_dirs (`Optional[Union[str, list[str]]]`, defaults to `None`):
            The directories to search for the history of runtime instances.
        debug (`bool`, optional):
            Whether to enable the debug mode. Defaults to False.
    """

    # Set the history directories
    if isinstance(run_dirs, str):
        run_dirs = [run_dirs]

    global _RUNS_DIRS
    _RUNS_DIRS = run_dirs

    # Create the cache directory
    with app.app_context():
        db.create_all()

    if debug:
        app.logger.setLevel("DEBUG")
    else:
        app.logger.setLevel("INFO")

    # To be compatible with the old table schema, we need to check and convert
    # the id column of the message_table from INTEGER to VARCHAR.
    # _check_and_convert_id_type(str(_cachedb), "message_table")

    socketio.run(
        app,
        host=host,
        port=port,
        debug=debug,
        allow_unsafe_werkzeug=True,
    )


if __name__ == "__main__":
    init()

    # 1. 所有节点的入参和出参，统一到一个全局变量池子里，并且初始化时能够正确串联。
    # 2. API节点和python节点的封装和定义，完备代码实现。
