import os
import sys
import time
import yaml
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO
from flask_cors import CORS

sys.path.append('/agentscope/src')

app = Flask(__name__)

# 设置时区为东八区
os.environ['TZ'] = 'Asia/Shanghai'
if not os.name == 'nt':  # 检查是否为 Windows 系统
    time.tzset()

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
    with open('/agentscope/src/agentscope/aibigmodel_workflow/config.yaml', 'r') as file:
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
    RAG_URL = config['RAG_URL']
    LLM_URL = config['LLM_URL']
    LLM_TOKEN = config['LLM_TOKEN']
    SERVER_PORT = config['SERVER_PORT']

    SQLALCHEMY_DATABASE_URI = "{}+{}://{}:{}@{}:{}/{}?charset=utf8".format(DIALECT, DRIVER, USERNAME, PASSWORD, HOST,
                                                                           PORT,
                                                                           DATABASE)
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

# This will enable CORS for all routes
CORS(app)

_RUNS_DIRS = []