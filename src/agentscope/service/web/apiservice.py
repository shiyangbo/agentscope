# -*- coding: utf-8 -*-
"""Search question in the web"""
from typing import Any, Dict, Optional
import requests
import json as j

from agentscope.service.service_response import ServiceResponse
from agentscope.service.service_status import ServiceExecStatus
from loguru import logger

def api_request(
        method: str,
        url: str,
        auth: Optional[str] = None,
        api_key: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
) -> ServiceResponse:
    """
    发送API请求并返回响应。

    Args：
    method（`str`）：要使用的HTTP方法（例如，'GET'、'POST'）。
    url（`str`）：请求的url。
    auth（`Optional[str]`）：要在授权标头中使用的API密钥或令牌。
    api_key（`Optional[str]`）：要在参数中使用的api键。
    params（`Optional[Dict[str，Any]]`）：查询请求的参数。
    data（`Optional[Dict[str，Any]]`）：在请求正文中发送表单数据。
    json（`Optional[Dict[str，Any]]`）：在请求正文中发送的json数据。
    headers（`Optional[Dict[str，Any]]`）：请求中要包含的标头。
    **kwargs（`Any`）：传递给请求的其他关键字参数。

    返回：
    `ServiceResponse：具有“status”和“content”的响应对象。
    """
    if headers is None:
        headers = {}
    if auth:
        headers['Authorization'] = auth

    if params is None:
        params = {}
    if api_key:
        params['key'] = api_key

    if json and len(json) == 0:
        json = None

    try:
        resp = requests.request(
            method=method,
            url=url,
            params=params,
            data=data,
            json=json,
            headers=headers,
            timeout=30,  # 简单设置最大超时阈值成30秒
            verify=False,
            **kwargs,
        )
        resp.raise_for_status()  # Raise an error for bad responses
    except requests.RequestException as e:
        return ServiceResponse(ServiceExecStatus.ERROR, str(e))

    try:
        # Attempt to parse response as JSON
        content = resp.json()
    except ValueError:
        # If response is not JSON, return raw text
        content = resp.text
        # 这里我们约定，返回内容必须是json字符串，所以这里简单定义
        content = {'data': content}

    return ServiceResponse(ServiceExecStatus.SUCCESS, content)

def api_request_for_big_model(
        method: str,
        url: str,
        auth: Optional[str] = None,
        api_key: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
) -> ServiceResponse:
    """
    发送API请求并返回响应。

    Args：
    method（`str`）：要使用的HTTP方法（例如，'GET'、'POST'）。
    url（`str`）：请求的url。
    auth（`Optional[str]`）：要在授权标头中使用的API密钥或令牌。
    api_key（`Optional[str]`）：要在参数中使用的api键。
    params（`Optional[Dict[str，Any]]`）：查询请求的参数。
    data（`Optional[Dict[str，Any]]`）：在请求正文中发送表单数据。
    json（`Optional[Dict[str，Any]]`）：在请求正文中发送的json数据。
    headers（`Optional[Dict[str，Any]]`）：请求中要包含的标头。
    **kwargs（`Any`）：传递给请求的其他关键字参数。

    返回：
    `ServiceResponse：具有“status”和“content”的响应对象。
    """
    if headers is None:
        headers = {}
    if auth:
        headers['Authorization'] = auth

    if params is None:
        params = {}
    if api_key:
        params['key'] = api_key

    if json and len(json) == 0:
        json = None

    try:
        resp = requests.request(
            method=method,
            url=url,
            params=params,
            data=j.dumps(data),
            json=json,
            headers=headers,
            timeout=60,
            verify=False,
            stream=False,
            **kwargs,
        )
        resp.raise_for_status()  # Raise an error for bad responses
    except requests.RequestException as e:
        return ServiceResponse(ServiceExecStatus.ERROR, str(e))

    try:
        # Attempt to parse response as JSON
        content = resp.json()
    except ValueError as e:
        # If response is not JSON, return raw text
        logger.error(str(e))
        raise e

    return ServiceResponse(ServiceExecStatus.SUCCESS, content)


# 测试请求，示例：随机土味情话
if __name__ == '__main__':
    # 通用 API 调用示例
    response = api_request(
        method="GET",
        url="https://api.uomg.com/api/rand.qinghua?format=json",
    )
    # 通用 API 调用示例
    response2 = api_request(
        method="GET",
        url="https://restapi.amap.com/v5/place/text",
        params={"keywords": "雍和宫"},  # 查询参数
        api_key="77b5f0d102c848d443b791fd469b732d",  # 作为查询参数传递 API 密钥
    )
    print(response)
    print(response2)
