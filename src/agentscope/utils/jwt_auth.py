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

# key配置文件放在同目录下
config_path = os.path.join(os.path.dirname(__file__), 'config.yaml')

with open(config_path, 'r') as config_file:
    config = yaml.safe_load(config_file)

SECRET_KEY = config['secret_key']


# 按照后端代码model/request/jwt.go，制定自定义声明
class CustomClaims:
    def __init__(self, user_id, user_type, username, nickname, buffer_time, iss, nbf, exp=None, sub=None):
        self.user_id = user_id  # 用户ID
        self.user_type = user_type  # 用户类型
        self.username = username  # 用户名
        self.nickname = nickname  # 昵称
        self.buffer_time = buffer_time  # 缓冲时间
        self.exp = exp  # 过期时间
        self.iss = iss
        self.nbf = nbf  # 开始生效时间
        self.sub = sub  # JWT主体 (subject)

    def to_dict(self):
        return {
            "user_id": self.user_id,
            "user_type": self.user_type,
            "username": self.username,
            "nickname": self.nickname,
            "bufferTime": self.buffer_time,
            "exp": self.exp,
            "iss": self.iss,
            "nbf": self.nbf,
            "sub": self.sub
        }


# 解析JWT
def parse_jwt_with_claims(token_input):
    try:
        # 解码JWT并验证签名
        decoded_token = jwt.decode(token_input,
                                   SECRET_KEY,
                                   algorithms=["HS256"],
                                   options={
                                       "require_exp": True,  # 必须有 exp
                                       "require_nbf": True   # 必须有 nbf
                                        })

        # 使用字典解包来简化claims的构造
        claims_custom = CustomClaims(**{
            "user_id": decoded_token.get("user_id"),
            "user_type": decoded_token.get("user_type"),
            "username": decoded_token.get("username"),
            "nickname": decoded_token.get("nickname"),
            "buffer_time": decoded_token.get("bufferTime"),
            "exp": decoded_token.get("exp"),
            "iss": decoded_token.get("iss"),
            "nbf": decoded_token.get("nbf"),
            "sub": decoded_token.get("sub"),
        })

        return claims_custom.to_dict(), None

    except (ExpiredSignatureError, InvalidSignatureError, ImmatureSignatureError, InvalidTokenError) as e:
        # 使用异常类名来动态返回错误信息
        error_map = {
            ExpiredSignatureError: {"code": 1001, "message": "Token is expired"},        # 过期
            InvalidSignatureError: {"code": 1000, "message": "TokenMalformed"},         # 签名无效
            ImmatureSignatureError: {"code": 1000, "message": "Token not active yet"},  # 尚未生效
            InvalidTokenError: {"code": 1000, "message": "Token is invalid"},           # Token无效
        }
        return None, error_map.get(type(e), {"code": 7, "message": "Unknown Token error"})


# if __name__ == '__main__':
#     token = 'Bearer eyJhbGiOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpZCI6IjEiLCJ1c2VybmFtZSI6ImFkbWluIiwibmlja25hbWUiOiJhZG1pbiIsImJ1ZmZlclRpbWUiOjE3MjYyODc0MTcsImV4cCI6MTcyNjMzNzgxNywiaXNzIjoiMSIsIm5iZiI6MTcyNjI4MDIxNywic3ViIjoid2ViIn0.qjkxmE_xF7kqhloIPOQkll3yUsKnIQ20MvgquDYKXjM'
#     token_in = token.replace('Bearer ', '')
#     claims, err = parse_jwt_with_claims(token_in)
#     print(claims)
#     print(err)
