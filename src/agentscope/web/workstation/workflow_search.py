import agentscope.aibigmodel_workflow.config as config

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

# 创建引擎
engine = create_engine(config.SQLALCHEMY_DATABASE_URI, echo=True)

# 创建一个配置类
Session = sessionmaker(bind=engine)

# 创建一个独立的会话
session = Session()

def get_private_cloud_tenant_id(workflow_id, plugin_id):
    result = None
    if workflow_id != '':
        sql = text('select tenant_id from llm_workflow_info where id = :workflow_id')
        result = session.execute(sql, {'workflow_id': workflow_id})
    elif plugin_id != '':
        sql = text('select tenant_id from llm_plugin_info where id = :plugin_id')
        result = session.execute(sql, {'plugin_id': plugin_id})
    else:
        return ''

    if result is None:
        return ''

    # 获取查询结果的第一行
    row = result.fetchone()
    if row is not None:
        return row['tenant_id']
    else:
        return None
