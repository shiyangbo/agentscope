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


from flask import Flask
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO


DIALECT = 'mysql'
DRIVER = 'pymysql'
USERNAME = 'root'
PASSWORD = '2292558Huawei'
HOST = '127.0.0.1'
PORT = '3306'
DATABASE = 'agentscope'

SQLALCHEMY_DATABASE_URI = "{}+{}://{}:{}@{}:{}/{}?charset=utf8".format(DIALECT, DRIVER, USERNAME, PASSWORD, HOST, PORT,
                                                                       DATABASE)
print(SQLALCHEMY_DATABASE_URI)
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = SQLALCHEMY_DATABASE_URI
app.config['SQLALCHEMY_POOL_SIZE'] = 5
app.config['SQLALCHEMY_MAX_OVERFLOW'] = 10
app.config['SQLALCHEMY_POOL_TIMEOUT'] = 30
app.config['SQLALCHEMY_POOL_RECYCLE'] = 3600
app.config['SQLALCHEMY_ECHO'] = False

db = SQLAlchemy()
db.init_app(app)

socketio = SocketIO(app)

# This will enable CORS for all route
CORS(app)

_RUNS_DIRS = []


class _ExecuteTable(db.Model):  # type: ignore[name-defined]
    """Execute workflow."""
    __tablename__ = "execute_info"
    execute_id = db.Column(db.String(100), primary_key=True)  # 运行ID
    execute_result = db.Column(db.Text)


class _WorkflowTable(db.Model):  # type: ignore[name-defined]
    """Workflow store table."""
    __tablename__ = "workflow_info"
    id = db.Column(db.String(100), primary_key=True)  # 用户ID
    config_name = db.Column(db.String(100))
    config_content = db.Column(db.Text)


class _PluginTable(db.Model):  # type: ignore[name-defined]
    """Plugin table."""
    __tablename__ = "plugin_info"
    __table_args__ = {'extend_existing': True}
    id = db.Column(db.String(100), primary_key=True)  # 用户ID
    plugin_name = db.Column(db.String(100))  # 插件名称
    model_name = db.Column(db.String(100))  # 插件英文名称
    plugin_desc = db.Column(db.Text)  # 插件描述
    plugin_config = db.Column(db.Text)  # 插件dag配置文件
    plugin_field = db.Column(db.String(100))  # 插件领域
    plugin_desc_config = db.Column(db.Text)  # 插件描述配置文件


def _check_and_convert_id_type(db_path: str, table_name: str) -> None:
    """Check and convert the type of the 'id' column in the specified table
    from INTEGER to VARCHAR.

    Args:
        db_path (str): The path of the SQLite database file.
        table_name (str): The name of the table to be checked and converted.
    """

    if not os.path.exists(db_path):
        return

    # Connect to the SQLite database
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        # Obtain the table structure information
        cursor.execute(f"PRAGMA table_info({table_name});")
        columns = cursor.fetchall()

        # Look for the type of the 'id' column
        id_column = [col for col in columns if col[1] == "id"]
        if not id_column:
            return

        id_type = id_column[0][2].upper()
        if id_type in ["VARCHAR", "TEXT"]:
            return

        if id_type == "INTEGER":
            # Temporary table name
            temp_table_name = table_name + "_temp"

            # Create a new table and change the type of the 'id' column to
            # VARCHAR
            create_table_sql = f"CREATE TABLE {temp_table_name} ("
            for col in columns:
                col_type = "VARCHAR" if col[1] == "id" else col[2]
                create_table_sql += f"{col[1]} {col_type}, "
            create_table_sql = create_table_sql.rstrip(", ") + ");"

            cursor.execute(create_table_sql)

            # Copy data and convert the value of the 'id' column to a string
            column_names = ", ".join([col[1] for col in columns])
            column_values = ", ".join(
                [
                    f"CAST({col[1]} AS VARCHAR)" if col[1] == "id" else col[1]
                    for col in columns
                ],
            )
            cursor.execute(
                f"INSERT INTO {temp_table_name} ({column_names}) "
                f"SELECT {column_values} FROM {table_name};",
            )

            # Delete the old table
            cursor.execute(f"DROP TABLE {table_name};")

            # Rename the new table
            cursor.execute(
                f"ALTER TABLE {temp_table_name} RENAME TO {table_name};",
            )

            conn.commit()

    except sqlite3.Error as e:
        print(f"SQLite error: {e}")
    finally:
        conn.close()


def _remove_file_paths(error_trace: str) -> str:
    """
    Remove the real traceback when exception happens.
    """
    path_regex = re.compile(r'File "(.*?)(?=agentscope|app\.py)')
    cleaned_trace = re.sub(path_regex, 'File "[hidden]/', error_trace)

    return cleaned_trace


def _convert_to_py(  # type: ignore[no-untyped-def]
        content: str,
        **kwargs,
) -> Tuple:
    """
    Convert json config to python code.
    """
    from agentscope.web.workstation.workflow_dag import build_dag

    try:
        cfg = json.loads(content)
        logger.info(f"cfg {cfg}")
        return "True", build_dag(cfg).compile(**kwargs)
    except Exception as e:
        return "False", _remove_file_paths(
            f"Error: {e}\n\n" f"Traceback:\n" f"{traceback.format_exc()}",
        )


@app.route("/convert-to-py", methods=["POST"])
def _convert_config_to_py() -> Response:
    """
    Convert json config to python code and send back.
    """
    try:
        content = request.json.get("data")
        status, py_code = _convert_to_py(content)
        return jsonify(py_code=py_code, is_success=status)
    except Exception as e:
        return jsonify({"code": 400, "message": repr(e)})


def _cleanup_process(proc: subprocess.Popen) -> None:
    """Clean up the process for running application started by workstation."""
    proc.wait()
    app.logger.debug(f"The process with pid {proc.pid} is closed")


@app.route("/convert-to-py-and-run", methods=["POST"])
def _convert_config_to_py_and_run() -> Response:
    """
    Convert json config to python code and run.
    """
    content = request.json.get("data")
    studio_url = request.url_root.rstrip("/")
    run_id = _runtime.generate_new_runtime_id()
    logger.info(f"Loading configs from {content}")
    status, py_code = _convert_to_py(
        content,
        runtime_id=run_id,
        studio_url=studio_url,
    )

    if status == "True":
        try:
            with tempfile.NamedTemporaryFile(
                    delete=False,
                    suffix=".py",
                    mode="w+t",
            ) as tmp:
                tmp.write(py_code)
                tmp.flush()
                proc = subprocess.Popen(  # pylint: disable=R1732
                    ["python", tmp.name],
                )
                threading.Thread(target=_cleanup_process, args=(proc,)).start()
        except Exception as e:
            status, py_code = "False", _remove_file_paths(
                f"Error: {e}\n\n" f"Traceback:\n" f"{traceback.format_exc()}",
            )
    return jsonify(py_code=py_code, is_success=status, run_id=run_id)


# 发布调试成功的workflow
@app.route("/plugin/publish", methods=["POST"])
def plugin_publish() -> Response:
    id = uuid.uuid4()
    data = request.json.get("data")
    plugin_config = json.dumps(data["pluginConfig"])
    # 数据库存储
    plugin_desc_config = plugin_desc_config_generator(data)
    plugin_desc_config = json.dumps(plugin_desc_config)
    try:
        db.session.add(
            _PluginTable(
                id=str(id),
                plugin_name=data["pluginName"],
                model_name=data["modelName"],
                plugin_desc=data["pluginDesc"],
                plugin_config=plugin_config,
                plugin_field=data["pluginField"],
                plugin_desc_config=plugin_desc_config
            ),
        )
        db.session.commit()
    except SQLAlchemyError as e:
        db.session.rollback()
        raise
    # 调用agent智能体接口，将插件进行注册

    return jsonify({"code": 0, "message": "Workflow file published successfully"})


# 已经发布的workflow直接运行
@app.route("/plugin/run", methods=["POST"])
def plugin_run() -> Response:
    """
    Input query data and get response.
    """
    # 用户输入的data信息，包含start节点所含信息，config文件存储地址
    content = request.json.get("data")
    plugin_name = request.json.get("pluginName")
    plugin = _PluginTable.query.filter_by(plugin_name=plugin_name).first()
    if not plugin:
        abort(400, f"plugin [{plugin_name}] not exists")

    try:
        # 存入数据库的数据为前端格式，需要转换为后端可识别格式
        config = json.loads(plugin.plugin_config)
        converted_config = workflow_format_convert(config)
        dag = build_dag(converted_config)
    except Exception as e:
        return jsonify({"code": 400, "message": repr(e)})

    # 调用运行dag
    start_time = time.time()
    result, nodes_result = dag.run_with_param(content, config)
    end_time = time.time()
    executed_time = round(end_time - start_time, 3)
    # 获取workflow与各节点的执行结果
    execute_status = 'success' if all(node['node_status'] == 'success' for node in nodes_result) else 'failed'
    execute_result = get_workflow_running_result(nodes_result, dag.uuid, execute_status, str(executed_time))
    if not execute_result:
        abort(400, f"execute result [{dag.uuid}] not exists")
    execute_result = json.dumps(execute_result)
    # 数据库存储
    try:
        db.session.add(
            _ExecuteTable(
                execute_id=dag.uuid,
                execute_result=execute_result,
            ),
        )
        db.session.commit()
    except SQLAlchemyError as e:
        db.session.rollback()
        raise e
    logger.info(f"execute_result: {execute_result}")
    return jsonify(code=0, result=result, executeID=dag.uuid)


@app.route("/node/run", methods=["POST"])
def node_run() -> Response:
    """
    Input query data and get response.
    """
    # 用户输入的data信息，包含start节点所含信息，config文件存储地址
    content = request.json.get("data")
    node = request.json.get("nodeSchema")

    try:
        # 使用node_id, 获取需要运行的node配置
        node_config = node_format_convert(node)
        dag = build_dag(node_config)
    except Exception as e:
        return jsonify({"code": 400, "message": repr(e)})

    # content中的data内容
    result, _ = dag.run_with_param(content, node_config)
    return jsonify(code=0, result=result)


# 画布中的workflow，调试运行
@app.route("/workflow/run", methods=["POST"])
def workflow_run() -> Response:
    """
    Input query data and get response.
    """
    # 用户输入的data信息，包含start节点所含信息，config文件存储地址
    content = request.json.get("data")
    workflow_schema = request.json.get("workflowSchema")
    logger.info(f"workflow_schema: {workflow_schema}")

    try:
        # 存入数据库的数据为前端格式，需要转换为后端可识别格式
        converted_config = workflow_format_convert(workflow_schema)
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
    execute_result = get_workflow_running_result(nodes_result, dag.uuid, execute_status, str(executed_time))
    # 需要持久化
    logger.info(f"execute_result: {execute_result}")
    execute_result = json.dumps(execute_result)
    if not execute_result:
        abort(400, f"execute result [{dag.uuid}] not exists")
    # 数据库存储
    try:
        db.session.add(
            _ExecuteTable(
                execute_id=dag.uuid,
                execute_result=execute_result,
            ),
        )
        db.session.commit()
    except SQLAlchemyError as e:
        db.session.rollback()
        raise e

    return jsonify(code=0, result=result, executeID=dag.uuid)


@app.route("/workflow/save", methods=["POST"])
def workflow_save() -> Response:
    """
    Save the workflow JSON data to the local user folder.
    """
    # user_login = session.get("user_login", "local_user")
    # user_dir = os.path.join(_cache_dir)
    # 之后用用户_id替代
    id = uuid.uuid4()
    # if not os.path.exists(user_dir):
    #     os.makedirs(user_dir)

    # request 参数获取
    data = request.json
    filename = data.get("filename")
    workflow_str = data.get("workflow")
    if not filename:
        return jsonify({"code": 400, "message": "Filename is required"})

    try:
        workflow = json.loads(workflow_str)
        if not isinstance(workflow, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        return jsonify({"code": 400, "message": "Invalid workflow data"})

    # 数据库存储
    try:
        db.session.add(
            _WorkflowTable(
                id=str(id),
                config_name=filename,
                config_content=workflow_str,
            ),
        )
        db.session.commit()
    except SQLAlchemyError as e:
        db.session.rollback()
        raise e

    return jsonify({"code": 0, "message": "Workflow file saved successfully"})


@app.route("/workflow/get", methods=["GET"])
def workflow_get() -> tuple[Response, int] | Response:
    """
    Reads and returns workflow data from the specified JSON file.
    """
    data = request.json
    filename = data.get("filename")
    execute_id = data.get("id")
    if not filename or not execute_id:
        return jsonify({"error": "Filename and id is required"}), 400

    workflow_config = _WorkflowTable.query.filter_by(id=execute_id).first()
    if not workflow_config:
        abort(400, f"workflow_config [{execute_id}] not exists")

    return jsonify(
        {
            "code": 0,
            "result": workflow_config.config_content,
            "message": ""
        },
    )


@app.route("/workflow/status", methods=["GET"])
def workflow_get_process() -> tuple[Response, int] | Response:
    """
    Reads and returns workflow process results from the specified JSON file.
    """
    data = request.json
    execute_id = data.get("executeID")

    workflow_result = _ExecuteTable.query.filter_by(execute_id=execute_id).first()
    if not workflow_result:
        abort(400, f"workflow_result [{execute_id}] not exists")

    workflow_result.execute_result = json.loads(workflow_result.execute_result)
    return jsonify(
        {
            "code": 0,
            "result": workflow_result.execute_result,
            "message": ""
        },
    )


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
        "name_for_model": data["modelName"],
        "desc_for_human": data["pluginDesc"],
        "desc_for_model": data["pluginDesc"],
        "field": data["pluginField"],
        "question_example": data["pluginQuestionExample"],
        "answer_example": data["modelName"],
        "confirm_required": "false",
        "api_info": {
            "url": "http://127.0.0.1:5001/plugin/run",  # 后续改为服务部署的url地址
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


def init(
        host: str = "127.0.0.1",
        port: int = 5001,
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

    # TODO, 增加全局变量池，方便保存所有入参和出参变量

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
