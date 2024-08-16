# -*- coding: utf-8 -*-
"""Search question in the web"""
from typing import Any, Dict, Optional, Union
import requests
from agentscope.service.service_response import ServiceResponse
from agentscope.service.service_status import ServiceExecStatus


def api_request(
        method: str,
        url: str,
        auth: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
) -> ServiceResponse:
    """
    Sends an API request and returns the response.

    Args:
        method (`str`): The HTTP method to use (e.g., 'GET', 'POST').
        url (`str`): The URL for the request.
        auth (`Optional[str]`): The API key or token to be used in the Authorization header.
        params (`Optional[Dict[str, Any]]`): Query parameters for the request.
        data (`Optional[Dict[str, Any]]`): Form data to send in the body of the request.
        json (`Optional[Dict[str, Any]]`): JSON data to send in the body of the request.
        headers (`Optional[Dict[str, Any]]`): Headers to include in the request.
        **kwargs (`Any`): Additional keyword arguments passed to the request.

    Returns:
        `ServiceResponse`: A response object with `status` and `content`.
    """
    if headers is None:
        headers = {}
    if auth:
        headers['Authorization'] = auth

    try:
        resp = requests.request(
            method=method,
            url=url,
            params=params,
            data=data,
            json=json,
            headers=headers,
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

    return ServiceResponse(ServiceExecStatus.SUCCESS, content)


# 测试请求，示例：随机土味情话
if __name__ == '__main__':
    # 通用 API 调用示例
    response = api_request(
        method="GET",
        url="https://api.uomg.com/api/rand.qinghua?format=json",
    )
    print(response.content)
