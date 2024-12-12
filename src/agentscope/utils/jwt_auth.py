import jwt
from jwt.exceptions import (
    DecodeError,
    ExpiredSignatureError,
    InvalidSignatureError,
    InvalidTokenError,
    ImmatureSignatureError
)
import yaml
import os
import json
from flask import g
from typing import List, Optional, Tuple

# key配置文件放在同目录下
config_path = os.path.join(os.path.dirname(__file__), 'config.yaml')

with open(config_path, 'r') as config_file:
    config = yaml.safe_load(config_file)

SECRET_KEY = config['secret_key']

# 定义云类型的常量
SIMPLE_CLOUD = ""
PRIVATE_CLOUD = "PRIVATE_CLOUD"


# 按照后端代码model/request/jwt.go，制定自定义声明

class UserInfo:
    def __init__(self, id: str, username: str, nickname: str):
        self.id = id  # 用户ID
        self.username = username  # 用户名
        self.nickname = nickname  # 昵称


class UserPermissionList:
    def __init__(self, tenant_id: str, tenant_name: str, region_id: str):
        self.tenant_id = tenant_id  # 云账号ID
        self.tenant_name = tenant_name  # 云账号显示名
        self.region_id = region_id  # 资源池ID


class TenantInfo:
    def __init__(self, master_tenant_id: Optional[str], master_tenant_name: Optional[str], permission_type: int,
                 permission_list: List[UserPermissionList]):
        self.master_tenant_id = master_tenant_id  # 用户归属租户ID
        self.master_tenant_name = master_tenant_name  # 用户归属租户名称
        self.permission_type = permission_type  # 用户的角色类型
        self.permission_list = permission_list  # 用户归属租户下的云账号列表


class CustomClaims:
    def __init__(self, user_info: UserInfo, user_type: int, buffer_time: int, tenant_info: TenantInfo, iss: str,
                 nbf: int, exp: Optional[int] = None, sub: Optional[str] = None):
        self.user_info = user_info  # 基础用户信息
        self.user_type = user_type  # 对私有云场景，固定userType=2
        self.buffer_time = buffer_time  # 缓冲时间
        self.tenant_info = tenant_info  # 私有云/行业云的租户信息
        self.iss = iss
        self.nbf = nbf
        self.exp = exp
        self.sub = sub

    def to_dict(self):
        return {
            "user_info": self.user_info.__dict__,
            "user_type": self.user_type,
            "buffer_time": self.buffer_time,
            "tenant_info": self.tenant_info.__dict__,
            "iss": self.iss,
            "nbf": self.nbf,
            "exp": self.exp,
            "sub": self.sub,
        }


# 解析JWT
def parse_jwt_with_claims(token_input: str):
    try:
        # 解码JWT并验证签名
        decoded_token = jwt.decode(token_input,
                                   SECRET_KEY,
                                   algorithms=["HS256"],
                                   options={
                                       "require_exp": True,  # 必须有 exp
                                       "require_nbf": True  # 必须有 nbf
                                   },
                                   leeway=120)

        # 使用字典解包来简化claims的构造
        claims_custom = CustomClaims(
            user_info=UserInfo(
                id=decoded_token.get("id"),
                username=decoded_token.get("username"),
                nickname=decoded_token.get("nickname"),
            ),
            user_type=decoded_token.get("userType"),
            buffer_time=decoded_token.get("bufferTime"),
            tenant_info=TenantInfo(
                master_tenant_id=decoded_token.get("masterTenantId"),
                master_tenant_name=decoded_token.get("masterTenantName"),
                permission_type=decoded_token.get("staffPermissionType"),
                permission_list=[
                    UserPermissionList(
                        tenant_id=perm.get("tenantId"),
                        tenant_name=perm.get("tenantName"),
                        region_id=perm.get("regionId"),
                    )
                    for perm in decoded_token.get("permissionList", [])
                ],
            ),
            iss=decoded_token.get("iss"),
            nbf=decoded_token.get("nbf"),
            exp=decoded_token.get("exp"),
            sub=decoded_token.get("sub"),
        )

        return claims_custom.to_dict(), None

    except (ExpiredSignatureError, InvalidSignatureError, ImmatureSignatureError, InvalidTokenError) as e:
        # 使用异常类名来动态返回错误信息
        error_map = {
            ExpiredSignatureError: {"code": 1001, "message": "Token is expired"},  # 过期
            InvalidSignatureError: {"code": 1000, "message": "TokenMalformed"},  # 签名无效
            ImmatureSignatureError: {"code": 1000, "message": "Token not active yet"},  # 尚未生效
            InvalidTokenError: {"code": 1000, "message": "Token is invalid"},  # Token无效
        }
        return None, error_map.get(type(e), {"code": 1000, "message": "Unknown Token error"})


def get_user_id():
    # 从 g 对象中获取用户ID
    user_id = g.claims.get("user_info")["id"]
    return user_id


def get_cloud_type():
    # 从 g 对象中获取云类型
    user_type = g.claims.get("user_type")
    permission_list = g.claims.get("tenant_info")['permission_list']
    if user_type == 2 and len(permission_list) > 0:
        return PRIVATE_CLOUD
    else:
        return SIMPLE_CLOUD


def get_tenant_ids():
    # 从 g 对象中获取租户ID
    permission_list = g.claims.get("tenant_info", {}).get('permission_list', [])
    tenant_ids = []
    for permission in permission_list:
        tenant_id = permission.tenant_id  # 直接使用点操作符访问属性
        if tenant_id:
            tenant_ids.append(tenant_id)

    return tenant_ids


if __name__ == '__main__':
    token = 'Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpZCI6IjRmMmYxYTdkLWUxM2EtNGZiNy05YjY2LTgzMTg3ZDU1MmRkZiIsInVzZXJUeXBlIjowLCJ1c2VybmFtZSI6InpnX3d1bGl1Iiwibmlja25hbWUiOiJ6Z193dWxpdSIsImJ1ZmZlclRpbWUiOjE3MzA5ODMyNDcsImV4cCI6MTczMzU2ODA0NywianRpIjoiNDg0MDMyNTQ4NzQ1NGUyZmE2MDNmZGRkNWJmYTYwYmYiLCJpc3MiOiI0ZjJmMWE3ZC1lMTNhLTRmYjctOWI2Ni04MzE4N2Q1NTJkZGYiLCJuYmYiOjE3MzA5NzYwNDcsInN1YiI6ImtvbmcifQ.3-HNv5BV6d6ZGNWuqmxlV7F7bVrikytZk0PIle7JSF8'
    token_in = token.replace('Bearer ', '')
    claims, err = parse_jwt_with_claims(token_in)
    print(claims)
    print(err)
