from config import db
from sqlalchemy import or_

class _ExecuteTable(db.Model):  # type: ignore[name-defined]
    """Execute workflow."""
    __tablename__ = "llm_execute_info"
    execute_id = db.Column(db.String(100), primary_key=True)  # 运行ID
    execute_result = db.Column(db.Text(length=2 ** 32 - 1))
    user_id = db.Column(db.String(100))  # 用户ID
    executed_time = db.Column(db.DateTime)
    workflow_id = db.Column(db.String(100))  # workflowID
    tenant_id = db.Column(db.String(100))  # TenantID


class _WorkflowTable(db.Model):  # type: ignore[name-defined]
    """Workflow store table."""
    __tablename__ = "llm_workflow_info"
    id = db.Column(db.String(100), primary_key=True)  # workflowID
    user_id = db.Column(db.String(100))  # 用户ID
    config_name = db.Column(db.String(100))
    config_en_name = db.Column(db.String(100))
    config_desc = db.Column(db.Text)
    dag_content = db.Column(db.Text, default='{}')
    status = db.Column(db.String(10))
    updated_time = db.Column(db.DateTime)
    execute_status = db.Column(db.String(10))
    tenant_id = db.Column(db.String(100))  # TenantID
    example_flag = db.Column(db.Integer, default=0)  # 样例ID，0表示客户创建，1表示预置样例

    def to_dict(self):
        return {
            'id': self.id,
            'userID': self.user_id,
            'configName': self.config_name,
            'configENName': self.config_en_name,
            'configDesc': self.config_desc,
            'status': self.status,
            'example_flag': self.example_flag,
            'updatedTime': self.updated_time.strftime('%Y-%m-%d %H:%M:%S')
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
    tenant_id = db.Column(db.String(100))  # TenantID


def fetch_records_by_filters(table, columns=None, method='first', **kwargs):
    """
    Get workflow by filters.
    Args:
        table: The database table to select
        columns (list): The columns to select.
        method (str): The method to use for the query ('all' or 'first').
        **kwargs: Keyword arguments to filter the query.
    Returns:
        list or _PluginTable: The plugins that match the filters or None if no match is found.
    """
    query = table.query
    if columns:
        query = query.with_entities(*columns)

    # 构建过滤条件
    filters = []
    for key, value in kwargs.items():
        if key == 'or_conditions':
            continue  # 跳过 or_conditions，稍后处理
        if key.endswith('__in'):
            filters.append(getattr(table, key[:-4]).in_(value))
        elif key.endswith('__like'):
            filters.append(getattr(table, key[:-6]).like(value))
        else:
            filters.append(getattr(table, key) == value)

    # 处理 or_conditions
    if 'or_conditions' in kwargs:
        or_conditions = kwargs['or_conditions']
        filters.append(parse_or_conditions(table, or_conditions))

    # 应用过滤条件
    query = query.filter(*filters)

    if method == 'all':
        return query.all()
    elif method == 'first':
        return query.first()
    else:
        raise ValueError(f"Invalid method '{method}'. Use 'all' or 'first'.")


def parse_or_conditions(table, or_conditions):
    """
    Parse OR conditions and return a SQLAlchemy query condition.

    Args:
        table: The database table to select from.
        or_conditions (tuple): A tuple containing OR conditions.

    Returns:
        sqlalchemy.sql.elements.BinaryExpression: A SQLAlchemy query condition.
    """
    conditions = []
    for condition in or_conditions:
        if isinstance(condition, tuple):
            field, value = condition
            if isinstance(value, list):
                conditions.append(getattr(table, field).in_(value))
            else:
                conditions.append(getattr(table, field) == value)
        else:
            raise ValueError(f"Invalid OR condition: {condition}")
    return or_(*conditions)

