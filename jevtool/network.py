from __future__ import annotations
import threading
import time
from urllib.parse import urlparse
import requests
from .config import validate_endpoint
from .audit import record, clean, exception_details, register_secrets, traced

class ApiError(RuntimeError):
    pass
class Cancelled(RuntimeError):
    pass

class JsonHttp:
    """Bounded retries only for transient errors. Never log keys or response bodies."""
    def __init__(self, cfg: dict, stop: threading.Event | None = None, session=None, observer=None):
        self.cfg = cfg
        self.stop = stop or threading.Event()
        self.session = session or requests.Session()
        self.observer = observer
        self.context = {}

    def check_stop(self):
        if self.stop.is_set():
            raise Cancelled('操作已停止。')

    @traced('api.call')
    def post(self, url: str, key: str, *, payload=None, files=None, data=None) -> dict:
        register_secrets({'vision': {'api_key': key}})
        validate_endpoint(url)
        if not key or not key.strip():
            raise ApiError('缺少 API Key；请在设置页填写。')
        headers = {'Authorization': f'Bearer {key.strip()}'}
        if payload is not None:
            headers['Content-Type'] = 'application/json'
        attempts = self.cfg['attempts']
        for attempt in range(attempts):
            self.check_stop()
            host = urlparse(url).hostname or ''
            provider = ('minimax' if 'minimax' in host else 'jev' if host.endswith('typesafe.ai') else 'other')
            started = time.monotonic()
            metadata = {**self.context, 'provider': provider,
                        'model': (payload or {}).get('model', ''), 'attempt': attempt + 1,
                        'endpoint': urlparse(url)._replace(query='', fragment='').geturl(),
                        'subscription': key.strip().startswith('sk-cp'),
                        'priority': (payload or {}).get('service_tier') == 'priority'}
            evidence_id = record('api.request', details={**metadata, 'request': payload, 'form': data})
            call_id = self.observer('start', metadata) if self.observer else None
            def report(status, code=None, result=None, raw=None, error=None):
                record('api.response', status, {**metadata, 'request_seq': evidence_id, 'call_id': call_id, 'http_status': code, 'raw': raw, 'response': result, 'error': exception_details(error) if error else None}, elapsed=time.monotonic()-started)
                if self.observer:
                    self.observer('finish', {**metadata, 'id': call_id, 'status': status,
                        'http_status': code, 'elapsed': time.monotonic() - started,
                        'usage': result.get('usage') if isinstance(result, dict) else None})
            # Rewind file uploads before retrying.
            if files:
                for value in files.values():
                    f = value[1] if isinstance(value, tuple) else value
                    if hasattr(f, 'seek'):
                        f.seek(0)
            delay = min(2 ** attempt, 8)
            try:
                response = self.session.post(url, headers=headers, json=payload, files=files, data=data,
                    timeout=(self.cfg['connect_timeout'], self.cfg['read_timeout']), allow_redirects=False)
            except requests.RequestException as error:
                report('network_unknown', error=error)
                if attempt + 1 == attempts:
                    raise ApiError('网络连接失败或超时；本次API结果未确认。') from None
            else:
                code = response.status_code
                if 200 <= code < 300:
                    try:
                        result = response.json()
                    except (ValueError, requests.exceptions.JSONDecodeError):
                        report('invalid_json', code, raw=getattr(response, 'text', ''))
                        raise ApiError('API 返回的不是有效 JSON。') from None
                    if not isinstance(result, dict):
                        report('invalid_structure', code, result=result)
                        raise ApiError('API 返回结构错误：需要 JSON 对象。')
                    report('response_received', code, result, raw=getattr(response, 'text', ''))
                    self.check_stop()
                    return result
                report('http_error', code, raw=getattr(response, 'text', ''))
                if code not in (429, 500, 502, 503, 504, 529):
                    hint = {401: 'API Key 无效或无权限', 403: '无访问权限', 404: '接口或模型地址不正确',
                            422: '参数与服务接口不匹配', 400: '服务拒绝参数，请核对模型/接口'}.get(code, '请求失败')
                    raise ApiError(f'HTTP {code}：{hint}。')
                try:
                    delay = min(15.0, max(delay, float(response.headers.get('Retry-After', '0'))))
                except ValueError:
                    pass
                if attempt + 1 == attempts:
                    raise ApiError(f'HTTP {code}：服务繁忙/限流；已停止重试。')
            record('api.retry_wait', details={**metadata, 'seconds': delay})
            if self.stop.wait(delay):
                record('api.cancelled', 'cancelled', metadata)
                raise Cancelled('操作已停止。')
        raise ApiError('API 请求未完成。')
