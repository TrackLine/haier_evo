from __future__ import annotations
import requests
import json
import time
import threading
import uuid
import socket
import weakref
from aiohttp import web
from enum import Enum
from datetime import datetime, timezone, timedelta
from tenacity import retry, stop_after_attempt, retry_if_exception_type, wait_fixed
from websocket import WebSocketApp, WebSocket
from requests.exceptions import ConnectionError, Timeout, HTTPError
from urllib.parse import urlparse, urljoin, parse_qs
from urllib3.exceptions import NewConnectionError
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode, SWING_OFF, PRESET_NONE
from homeassistant.components.http import HomeAssistantView
from .logger import _LOGGER
from .limits import ResettableLimits
from . import config as CFG # noqa
from . import const as C # noqa


class InvalidAuth(HomeAssistantError):
    """Error to indicate we cannot connect."""

class InvalidDevicesList(HomeAssistantError):
    """Error to indicate we cannot connect."""

class AuthError(HTTPError):
    pass

class AuthUserError(HTTPError):
    pass

class AuthValidationError(AuthError):
    pass

class AuthInternalError(AuthError):
    pass

class ManyRequestsError(HTTPError):
    pass


class SocketStatus(Enum):
    PRE_INITIALIZATION = 0
    INITIALIZING = 1
    INITIALIZED = 2
    NOT_INITIALIZED = 3


class HaierAPI(HomeAssistantView):
    url = "/api/haier_evo"
    name = "/api:haier_evo"
    requires_auth = False

    def __init__(self) -> None:
        self.haier = None

    # noinspection PyUnusedLocal
    async def get(self, request):
        if not getattr(self.haier, "allow_http", False):
            return web.Response(text="404: Not found", status=404, content_type="text/plain")
        return self.json(self.haier.to_dict())

    async def post(self, request):
        if not getattr(self.haier, "allow_http_post", False):
            return web.Response(text="404: Not found", status=404, content_type="text/plain")
        data = await request.json()
        self.haier.send_message(json.dumps(data))
        return self.json({"result": "success"})


class AuthResponse(object):

    def __init__(self, response: requests.Response):
        self.response = response
        self.json_data = response.json() or {}
        self.data = self.json_data.get("data") or {}
        self.error = self.json_data.get("error")
        self.token = self.data.get("token") or {}

    def __getattr__(self, item):
        if hasattr(self.response, item):
            return getattr(self.response, item)
        raise AttributeError(item)

    def __repr__(self) -> str:
        return self.response.__repr__()

    def raise_for_error(self) -> None:
        if self.error and isinstance(self.error, dict):
            validation = self.error.get("validation") or {}
            if message := validation.get('refreshToken'):
                # noinspection PyTypeChecker
                raise AuthValidationError(message, response=self)
            if message := validation.get('email'):
                # noinspection PyTypeChecker
                raise AuthUserError(message, response=self)
            if message := validation.get('password'):
                # noinspection PyTypeChecker
                raise AuthUserError(message, response=self)
            if message := self.error.get("message"):
                # noinspection PyTypeChecker
                raise AuthInternalError(message, response=self)
            # noinspection PyTypeChecker
            raise AuthError(str(self.error), response=self)
        return None

    @property
    def access_token(self) -> str | None:
        assert "accessToken" in self.token, f"Bad data: refreshToken not found"
        value = self.token["accessToken"]
        assert isinstance(value, str) and value, f"Bad token: {value!r}"
        return value

    @property
    def access_expire(self) -> datetime | None:
        assert "expire" in self.token, f"Bad data: expire not found"
        value = self.token["expire"]
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")

    @property
    def refresh_token(self) -> str | None:
        assert "refreshToken" in self.token, f"Bad data: refreshToken not found"
        value = self.token["refreshToken"]
        assert isinstance(value, str) and value, f"Bad token: {value!r}"
        return value

    @property
    def refresh_expire(self) -> datetime | None:
        assert "refreshExpire" in self.token, f"Bad data: refreshExpire not found"
        value = self.token["refreshExpire"]
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")


class Haier(object):

    http = HaierAPI()
    connect_limits = ResettableLimits(calls=1, period=5)
    common_limits = ResettableLimits(
        calls=C.COMMON_LIMIT_CALLS,
        period=C.COMMON_LIMIT_PERIOD,
    )
    auth_login_limits = ResettableLimits(
        calls=C.LOGIN_LIMIT_CALLS,
        period=C.LOGIN_LIMIT_PERIOD,
        max=C.LOGIN_LIMIT_MAX
    )
    auth_refresh_limits = ResettableLimits(
        calls=C.REFRESH_LIMIT_CALLS,
        period=C.REFRESH_LIMIT_PERIOD,
        max=C.REFRESH_LIMIT_MAX
    )

    def __init__(
        self,
        hass: HomeAssistant,
        email: str,
        password: str,
        region: str,
        http: bool = C.API_HTTP_ROUTE
    ) -> None:
        self._lock = threading.Lock()
        self._pull_data = None
        self._device_id = str(uuid.uuid4())
        self.hass: HomeAssistant = hass
        self.devices: list[HaierDevice] = []
        self.email: str = email
        self.password: str = password
        self.region: str = region
        self.allow_http: bool = http
        self.allow_http_post: bool = False
        self.token: str | None = None
        self.tokenexpire: datetime | None = None
        self.refreshtoken: str | None = None
        self.refreshexpire: datetime | None = None
        self.socket_app: WebSocketApp | None = None
        self.disconnect_requested = False
        self.socket_status: SocketStatus = SocketStatus.PRE_INITIALIZATION
        self.socket_thread = None
        self.reset_limits()
        self.register_view()

    def to_dict(self) -> dict:
        return {
            "socket_status": getattr(self.socket_status, "value", None),
            "backend_data": self._pull_data,
            "devices": [device.to_dict() for device in self.devices]
        }

    def load_tokens(self) -> None:
        filename = self.hass.config.path(C.DOMAIN)
        try:
            with open(filename, "r") as f:
                data = json.load(f)
            assert isinstance(data, dict), "Bad saved tokens file"
            self.token = data.get("token", None)
            tokenexpire = data.get("tokenexpire")
            self.tokenexpire = datetime.fromisoformat(tokenexpire) if tokenexpire else None
            self.refreshtoken = data.get("refreshtoken", None)
            refreshexpire = data.get("refreshexpire")
            self.refreshexpire = datetime.fromisoformat(refreshexpire) if refreshexpire else None
            _LOGGER.info(f"Loaded tokens file: {filename}")
        except FileNotFoundError:
            _LOGGER.warning(f"No tokens file: {filename}")
        except Exception as e:
            _LOGGER.error(f"Failed to load tokens file: {e}")

    def save_tokens(self) -> None:
        try:
            filename = self.hass.config.path(C.DOMAIN)
            with open(filename, "w") as f:
                json.dump({
                    "token": self.token,
                    "tokenexpire": str(self.tokenexpire) if self.tokenexpire else None,
                    "refreshtoken": self.refreshtoken,
                    "refreshexpire": str(self.refreshexpire) if self.refreshexpire else None,
                }, f)
        except Exception as e:
            _LOGGER.error(f"Failed to save tokens file: {e}")
        else:
            _LOGGER.debug(f"Saved tokens file: {filename}")

    def clear_tokens(self) -> None:
        self.token = None
        self.tokenexpire = None
        self.refreshtoken = None
        self.refreshexpire = None
        self.save_tokens()

    def reset_limits(self) -> None:
        self.connect_limits.reset()
        self.common_limits.reset()
        self.auth_login_limits.reset()
        self.auth_refresh_limits.reset()

    def get_http_resources(self) -> list:
        http = getattr(self.hass, "http", None)
        app = getattr(http, "app", None)
        router = getattr(app, "router", None)
        resources = getattr(router, "resources", None)
        return resources() if resources else []

    def register_view(self) -> None:
        if self.http.url not in (r.canonical for r in self.get_http_resources()):
            self.hass.http.register_view(self.http)
        self.http.haier = weakref.proxy(self)

    def unregister_view(self) -> None:
        self.http.haier = None

    def stop(self) -> None:
        self.disconnect_requested = True
        self.reset_limits()
        if self.socket_app is not None:
            self.socket_app.close()
        self.unregister_view()

    @common_limits.sleep_and_retry
    @common_limits
    def make_request(self, method: str, url: str, **kwargs) -> requests.Response:
        try:
            assert self.disconnect_requested is False, 'Service already stoped'
            # Setting a default timeout for requests
            kwargs.setdefault('timeout', C.API_TIMEOUT)
            headers = kwargs.setdefault('headers', {})
            headers.setdefault('User-Agent', "curl/7.81.0")
            headers.setdefault('Accept', "*/*")
            resp = requests.request(method, url, **kwargs)
            # _LOGGER.debug(resp.text)
            # Handling 429 Too Many Requests with retry
            if resp.status_code == 429:
                raise ManyRequestsError("429 Too Many Requests", response=resp)
            # Raise for other HTTP errors
            resp.raise_for_status()
            return resp
        except (ConnectionError, NewConnectionError, socket.gaierror) as e:
            _LOGGER.error(f"Network error occurred: {e}")
            raise e  # Re-raise to allow retry mechanisms to handle this
        except Timeout as e:
            _LOGGER.error(f"Request timed out: {e}")
            raise e
        except HTTPError as e:
            _LOGGER.error(f"HTTP error occurred: {e}")
            raise e

    @auth_login_limits.sleep_and_retry
    @auth_login_limits
    def auth_login(self) -> AuthResponse:
        try:
            path = urljoin(C.API_PATH, C.API_LOGIN.format(region=self.region))
            _LOGGER.debug(f"Logging in to {path}")
            response = AuthResponse(self.make_request('POST', path, data={
                'email': self.email,
                'password': self.password
            }))
            # _LOGGER.info(f"Login status code: {response.status_code}")
            response.raise_for_error()
        except ManyRequestsError as e:
            self.auth_login_limits.add_period(C.LOGIN_LIMIT_429)
            raise e
        except AuthInternalError as e:
            _LOGGER.error(str(e))
            self.auth_login_limits.add_period(C.LOGIN_LIMIT_500)
            response = e.response
        except AuthUserError as e:
            self.disconnect_requested = True
            raise e
        else:
            self.auth_login_limits.set_period()
        finally:
            self.auth_refresh_limits.reset()
        return response

    @auth_refresh_limits.sleep_and_retry
    @auth_refresh_limits
    def auth_refresh(self) -> AuthResponse:
        try:
            path = urljoin(C.API_PATH, C.API_TOKEN_REFRESH.format(region=self.region))
            _LOGGER.debug(f"Refreshing token in to {path}")
            response = AuthResponse(self.make_request('POST', path, data={
                'refreshToken': self.refreshtoken
            }))
            # _LOGGER.info(f"Refresh status code: {response.status_code}")
            response.raise_for_error()
        except ManyRequestsError as e:
            self.auth_refresh_limits.add_period(C.REFRESH_LIMIT_429)
            raise e
        except AuthValidationError as e:
            _LOGGER.error(str(e))
            self.clear_tokens()
            raise e
        except AuthInternalError as e:
            _LOGGER.error(str(e))
            self.auth_refresh_limits.add_period(C.REFRESH_LIMIT_500)
            response = e.response
        else:
            self.auth_refresh_limits.set_period()
        finally:
            self.auth_login_limits.reset()
        return response

    @retry(
        retry=retry_if_exception_type(AuthValidationError),
        stop=stop_after_attempt(2),
    )
    def login(self, refresh: bool = False) -> None:
        resp = None
        try:
            if refresh and self.refreshtoken:  # token refresh
                resp = self.auth_refresh()
            else:  # initial login
                resp = self.auth_login()
            assert resp, "No response from login"
            self.token = resp.access_token
            self.tokenexpire = resp.access_expire
            self.refreshtoken = resp.refresh_token
            self.refreshexpire = resp.refresh_expire
            self.save_tokens()
        except AuthValidationError as e:
            raise e
        except AssertionError as e:
            _LOGGER.error(f"Assertion error: {e}")
        except Exception as e:
            _LOGGER.error(
                f"Failed to login/refresh token, "
                f"response was: {resp}, "
                f"err: {e}"
            )
            raise InvalidAuth()
        else:
            _LOGGER.debug(f"Successful update tokens")

    def auth(self) -> None:
        with self._lock:
            tzinfo = timezone(timedelta(hours=+3.0))
            # tzinfo = datetime.now(timezone.utc).astimezone().tzinfo
            now = datetime.now(tzinfo)
            tokenexpire = self.tokenexpire or now
            refreshexpire = self.refreshexpire or now
            if self.token:
                if tokenexpire > now:
                    return None
                elif self.refreshtoken and refreshexpire > now:
                    # _LOGGER.debug(f"Token to be refreshed")
                    return self.login(refresh=True)
            # _LOGGER.debug(f"Token expired or empty")
            return self.login()

    def pull_data_from_api(self) -> dict:
        self.auth()
        response = None
        try:
            devices_path = urljoin(C.API_PATH, C.API_DEVICES.format(region=self.region))
            _LOGGER.debug(f"Getting devices, url: {devices_path}")
            response = requests.get(devices_path, headers={
                'X-Auth-Token': self.token,
                'User-Agent': 'evo-mobile',
                'Device-Id': self._device_id,
                'Content-Type': 'application/json'
            }, timeout=C.API_TIMEOUT)
            _LOGGER.debug(response.text)
            response.raise_for_status()
            data = response.json().get("data", {})
            assert isinstance(data, dict), f"Data is not dict: {data}"
            return data
        except Exception as e:
            _LOGGER.error(f"Failed to get devices {e}, response was: {response}")
            return {}

    @retry(
        retry=retry_if_exception_type(HTTPError),
        stop=stop_after_attempt(2),
    )
    def pull_device_data(self, device_mac: str) -> dict:
        self.auth()
        response = None
        try:
            status_url = C.API_STATUS.format(mac=device_mac)
            _LOGGER.debug(f"Getting initial status of device {device_mac}, url: {status_url}")
            response = requests.get(status_url, headers={
                'X-Auth-Token': self.token,
                'User-Agent': 'evo-mobile',
                'Device-Id': self._device_id,
                'Content-Type': 'application/json'
            }, timeout=C.API_TIMEOUT)
            _LOGGER.debug(f"Update device {device_mac} status code: {response.status_code}")
            _LOGGER.debug(response.text)
            response.raise_for_status()
            data = response.json()
            return data
        except Exception as e:
            _LOGGER.error(f"Failed to get status: {e}, response was: {response}")
            raise

    def pull_data(self) -> None:
        self._pull_data = data = self.pull_data_from_api()
        if not self._pull_data:
            raise InvalidDevicesList()
        need_container_id = "72a6d224-cb66-4e6d-b427-2e4609252684"
        presentation = data.setdefault("presentation", {})
        layout = presentation.setdefault("layout", {})
        containers = layout.setdefault("scrollContainer", [])

        def collect_from(items):
            for item in items:
                state_data = item.setdefault("state", "{}")
                state_json = (
                    json.loads(state_data)
                    if isinstance(state_data, str)
                    else state_data
                )
                devices = state_json.setdefault("items", [])
                for d in devices:
                    device_title = d.get('title', '')
                    device_link = d.get('action', {}).get('link', '')
                    parsed_link = urlparse(device_link)
                    query_params = parse_qs(parsed_link.query)
                    device_type = query_params.setdefault('type', ['UNKNOWN'])[0]
                    device_mac = query_params.get('deviceId', [''])[0]
                    device_mac = device_mac.replace('%3A', ':')
                    device_serial = query_params.get('serialNum', [''])[0]
                    device = HaierDevice.create(
                        haier=self,
                        device_type=device_type,
                        device_mac=device_mac,
                        device_serial=device_serial,
                        device_title=device_title,
                    )
                    self.devices.append(device)
                    _LOGGER.info(f"Added device: {device}")

        # 1) Пытаемся найти точный контейнер
        filtered = []
        for item in containers:
            component = item.setdefault("trackingData", {}).setdefault("component", {})
            component_id = component.setdefault("componentId", "")
            component_name = component.setdefault("componentName", "")
            if component_name == "deviceList" and component_id == need_container_id:
                filtered.append(item)
        if filtered:
            collect_from(filtered)
        # 2) Фолбэк: если ничего не нашли, берём все deviceList-контейнеры
        if not self.devices:
            any_device_lists = []
            for item in containers:
                component = item.setdefault("trackingData", {}).setdefault("component", {})
                if component.setdefault("componentName", "") == "deviceList":
                    any_device_lists.append(item)
            if any_device_lists:
                _LOGGER.warning("Fallback to any deviceList containers (exact containerId not found)")
                collect_from(any_device_lists)
        # 3) Последний фолбэк: рекурсивный поиск action.link с deviceId/serialNum/type по всему ответу
        if not self.devices:
            def walk_links(node):
                if isinstance(node, dict):
                    link = (((node.get("action") or {}).get("link")) or None)
                    if isinstance(link, str) and ("deviceId=" in link or "serialNum=" in link) and "type=" in link:
                        return [link]
                    result = []
                    for v in node.values():
                        result.extend(walk_links(v))
                    return result
                elif isinstance(node, list):
                    result = []
                    for v in node:
                        result.extend(walk_links(v))
                    return result
                return []

            links = walk_links(data)
            for device_link in links:
                parsed_link = urlparse(device_link)
                query_params = parse_qs(parsed_link.query)
                device_type = query_params.setdefault('type', ['UNKNOWN'])[0]
                device_mac = (query_params.get('deviceId', [''])[0] or '').replace('%3A', ':')
                device_serial = query_params.get('serialNum', [''])[0]
                device_title = query_params.get('title', [''])[0]
                if not device_mac:
                    continue
                device = HaierDevice.create(
                    haier=self,
                    device_type=device_type,
                    device_mac=device_mac,
                    device_serial=device_serial,
                    device_title=device_title or device_type,
                )
                self.devices.append(device)
                _LOGGER.info(f"Added device (fallback link): {device}")
        if len(self.devices) > 0:
            self.connect_in_thread()

    def get_device_by_id(self, id_: str) -> HaierDevice | None:
        return next(filter(
            lambda d: d.device_id == id_,
            self.devices
        ), None)

    def _init_ws(self) -> None:
        self.auth()
        url = urljoin(C.API_WS_PATH, self.token)
        if self.socket_app is None:
            self.socket_app = WebSocketApp(
                url=url,
                on_message=self._on_message,
                on_open=self._on_open,
                on_ping=self._on_ping,
                on_close=self._on_close,
            )
        else:
            self.socket_app.url = url

    # noinspection PyUnusedLocal
    def _on_message(self, ws: WebSocket, message: str) -> None:
        _LOGGER.debug(f"Received WSS message: {message}")
        message_dict: dict = json.loads(message)
        message_device = str(message_dict.get("macAddress")).lower()
        device = self.get_device_by_id(message_device)
        if device is None:
            _LOGGER.error(f"Got a message for a device we don't know about: {message_device}")
        else:
            device.on_message(message_dict)

    # noinspection PyMethodMayBeStatic,PyUnusedLocal
    def _on_open(self, ws: WebSocket) -> None:
        self.socket_status = SocketStatus.INITIALIZED
        _LOGGER.debug("Websocket opened")
        for device in self.devices:
            device.init_if_needed()

    # noinspection PyUnusedLocal
    def _on_ping(self, ws: WebSocket) -> None:
        self.socket_app.sock.pong()

    # noinspection PyMethodMayBeStatic,PyUnusedLocal
    def _on_close(self, ws: WebSocket, close_code: int, close_message: str) -> None:
        _LOGGER.debug(f"Websocket closed. Code: {close_code}, message: {close_message}")

    def _wait_websocket(self, timeout: float) -> None:
        current = time.time()
        while time.time() <= (current + timeout):
            if self.socket_status == SocketStatus.INITIALIZED:
                return
            time.sleep(0.1)

    def write_ha_state(self) -> None:
        for device in self.devices:
            device.write_ha_state()

    def connect_if_needed(self, timeout: float = 4.0) -> None:
        if self.socket_thread and self.socket_thread.is_alive():
            return self._wait_websocket(timeout)
        return self.connect_in_thread()

    def connect(self) -> None:
        self.socket_status = SocketStatus.NOT_INITIALIZED
        while not self.disconnect_requested:
            self.run_forever()
        _LOGGER.debug("Connection stoped")

    def connect_in_thread(self) -> None:
        self.socket_thread = thread = threading.Thread(target=self.connect)
        thread.daemon = True
        thread.start()

    @connect_limits.sleep_and_retry
    @connect_limits
    def run_forever(self) -> None:
        _LOGGER.debug(f"Connecting to websocket ({C.API_WS_PATH})")
        try:
            self.socket_status = SocketStatus.INITIALIZING
            self._init_ws()
            self.socket_app.run_forever(ping_interval=10)
        except Exception as e:
            _LOGGER.error(f"Error connecting to websocket: {e}")

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(2),
        retry_error_callback=lambda _: None,
        wait=wait_fixed(0.5),
    )
    def send_message(self, payload: str) -> None:
        _LOGGER.debug(f"Sending message: {payload}")
        try:
            self.socket_app.send(payload)
        except Exception as e:
            _LOGGER.warning(f"Failed to send message: {e}")
            self.connect_if_needed()
            raise e


class HaierDevice(object):

    def __init__(
        self,
        haier: Haier,
        device_mac: str,
        device_serial: str = None,
        device_title: str = None,
        backend_data: dict = None,
    ) -> None:
        self._haier = weakref.proxy(haier)
        self.device_id = device_mac
        self.device_serial = device_serial
        self.device_name = device_title
        self.device_model = "UNKNOWN"
        self.sw_version = None
        self._write_ha_state_callbacks = []
        self._available = True
        self._config = None
        self._status_data = backend_data

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"{self.device_id!r},"
            f"name={self.device_name!r},"
            f"serial={self.device_serial!r},"
            f"model={self.device_model!r},"
            f"config={self.config!r}"
            f")"
        )

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(C.DOMAIN, self.device_id)},
            name=self.device_name,
            sw_version=self.sw_version,
            model=self.device_model,
            manufacturer="Haier"
        )

    @property
    def device_mac(self) -> str:
        return self.device_id

    @property
    def available(self) -> bool:
        return self._available

    @available.setter
    def available(self, value: bool | str):
        if not isinstance(value, bool):
            self._available = False if str(value).upper() == 'OFFLINE' else True
        else:
            self._available = value

    @property
    def status_data(self) -> dict:
        return self._status_data

    @property
    def hass(self) -> HomeAssistant:
        return self._haier.hass

    @property
    def config(self) -> CFG.HaierDeviceConfig:
        return self._config

    @property
    def constraint(self) -> CFG.Constraint:
        return self.config.constraint

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "device_id": self.device_id,
            "device_mac": self.device_mac,
            "device_name": self.device_name,
            "device_serial": self.device_serial,
            "sw_version": self.sw_version,
            "config": self.config.to_dict() if self.config else None,
            "backend_data": self.status_data,
        }

    def _get_status(self, data: dict) -> dict:
        self._status_data = data = (data or {})
        info = data.setdefault("info", {})
        self.device_serial = info.setdefault("serialNumber", self.device_serial)
        device_model = info.setdefault("model", "AC")
        device_model = device_model.replace('-','').replace('/', '')[:11]
        self.device_model = device_model
        self.available = data.setdefault("status", "ONLINE")
        settings = data.setdefault("settings", {})
        self.device_name = settings.setdefault("name", {}).setdefault("name", self.device_name)
        self.sw_version = settings.setdefault('firmware', {}).setdefault('value', None)
        # read config and current values
        self._load_config_from_attributes(data)
        return data

    def _load_config_from_attributes(self, data: dict) -> None:
        pass

    def _set_attribute_value(self, code: str, value: str) -> None:
        pass

    def _handle_status_update(self, received_message: dict) -> None:
        message_statuses = received_message.get("payload", {}).get("statuses", [{}])
        for key, value in message_statuses[0]['properties'].items():
            self._set_attribute_value(key, value)
        self.available = True
        self.write_ha_state()

    def _handle_device_status_update(self, received_message: dict) -> None:
        status = received_message.get("payload", {}).get("status")
        self.available = status
        self.write_ha_state()

    def _handle_info(self, received_message: dict) -> None:
        payload = received_message.get("payload", {})
        self.sw_version = payload.get("swVersion") or self.sw_version

    def _send_message(self, message: dict) -> None:
        self._haier.send_message(json.dumps(message))

    def _send_commands(self, commands: list[dict]) -> None:
        self._send_group_command(commands)

    def _send_group_command(self, commands: list[dict]) -> None:
        trace = str(uuid.uuid4())
        self._send_message({
            "action": "operation",
            "macAddress": self.device_id,
            "commandName": self.config.command_name,
            "commands": commands,
            "trace": trace,
        })

    def _send_single_command(self, command: dict) -> None:
        trace = str(uuid.uuid4())
        self._send_message({
            "action": "command",
            "macAddress": self.device_id,
            "command": command,
            "trace": trace,
        })

    def init_if_needed(self) -> None:
        pass

    def get_commands(self, name: str, value: str | bool) -> list[dict]:
        value = str({True: "on", False: "off", None: "off"}.get(value, value))
        if custom := self.config.get_command_by_name(f"{name}_{value}"):
            return custom
        attr = self.config.get_attr_by_name(name)
        return self.constraint.apply([{
            "commandName": str(attr.code),
            "value": attr.get_item_code(value),
        }] if attr else [])

    def on_message(self, message_dict: dict) -> None:
        message_type = message_dict.get("event", "")
        if message_type == "status":
            self._handle_status_update(message_dict)
        elif message_type == "command_response":
            pass
        elif message_type == "info":
            self._handle_info(message_dict)
        elif message_type == "deviceStatusEvent":
            self._handle_device_status_update(message_dict)
        else:
            _LOGGER.warning(f"Got unknown message: {message_dict}")

    def write_ha_state(self) -> None:
        for callback in self._write_ha_state_callbacks:
            self.hass.loop.call_soon_threadsafe(callback)

    def add_write_ha_state_callback(self, callback) -> None:
        if callback not in self._write_ha_state_callbacks:
            self._write_ha_state_callbacks.append(callback)

    # noinspection PyMethodMayBeStatic
    def create_entities_climate(self) -> list:
        return []

    # noinspection PyMethodMayBeStatic
    def create_entities_switch(self) -> list:
        return []

    # noinspection PyMethodMayBeStatic
    def create_entities_select(self) -> list:
        return []

    # noinspection PyMethodMayBeStatic
    def create_entities_sensor(self) -> list:
        return []

    # noinspection PyMethodMayBeStatic
    def create_entities_binary_sensor(self) -> list:
        return []

    @classmethod
    def create(
        cls,
        haier: Haier,
        device_type: str,
        device_mac: str,
        device_serial: str = None,
        device_title: str = None,
    ) -> HaierDevice:
        device_cls = {
            "AC": HaierAC,
            "REF": HaierREF,
            "WM": HaierWM,
        }.get(device_type, cls)
        if device_cls is cls:
            _LOGGER.warning(f"Unknown device type: {device_type}")
        return device_cls(
            haier=haier,
            device_mac=device_mac,
            device_serial=device_serial,
            device_title=device_title,
            backend_data=haier.pull_device_data(device_mac),
        )


class HaierAC(HaierDevice):

    def __init__(
        self,
        backend_data: dict = None,
        **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self.current_temperature = 0
        self.target_temperature = 0
        self.status = None
        self.mode = None
        self.fan_mode = None
        self.swing_horizontal_mode = None
        self.swing_mode = None
        self._preset_mode = None
        self.min_temperature = 7
        self.max_temperature = 35
        self.light_on = True
        self.sound_on = True
        self.quiet_on = False
        self.turbo_on = False
        self.health_on = False
        self.comfort_on = False
        self.cleaning_on = False
        self.antifreeze_on = False
        self.autohumidity_on = False
        self.eco_sensor = None
        self._get_status(backend_data)
        self._inited = False

    @property
    def config(self) -> CFG.HaierACConfig:
        return self._config

    @property
    def preset_mode(self) -> str:
        if self._preset_mode not in ("none", "sleep", "boost"):
            return self._preset_mode
        elif self.quiet_on:
            return "sleep"
        elif self.turbo_on:
            return "boost"
        return "none"

    @preset_mode.setter
    def preset_mode(self, preset_mode: str) -> None:
        self._preset_mode = preset_mode

    def to_dict(self) -> dict:
        data = super().to_dict()
        data.update({
            "current_temperature": self.current_temperature,
            "target_temperature": self.target_temperature,
            "max_temperature": self.max_temperature,
            "min_temperature": self.min_temperature,
            "status": self.status,
            "mode": self.mode,
            "fan_mode": self.fan_mode,
            "swing_horizontal_mode": self.swing_horizontal_mode,
            "swing_mode": self.swing_mode,
            "preset_mode": self.preset_mode,
            "light_on": self.light_on,
            "sound_on": self.sound_on,
            "quiet_on": self.quiet_on,
            "turbo_on": self.turbo_on,
            "health_on": self.health_on,
            "comfort_on": self.comfort_on,
            "cleaning_on": self.cleaning_on,
            "antifreeze_on": self.antifreeze_on,
            "autohumidity_on": self.autohumidity_on,
            "eco_sensor": self.eco_sensor,
        })
        return data

    def _set_attribute_value(self, code: str, value: str) -> None:
        attr = self.config.get_attr_by_code(code)
        if not (attr and value is not None):
            return
        elif attr.name == "current_temperature":
            self.current_temperature = float(value)
        elif attr.name == "status":
            self.status = int(value)
        elif attr.name == "target_temperature":
            self.target_temperature = float(value)
        elif attr.name == "mode":
            self.mode = attr.get_item_name(value)
        elif attr.name == "fan_mode":
            self.fan_mode = attr.get_item_name(value)
        elif attr.name == "swing_horizontal_mode":
            self.swing_horizontal_mode = attr.get_item_name(value)
        elif attr.name == "swing_mode":
            self.swing_mode = attr.get_item_name(value)
        elif attr.name == "light":
            self.light_on = parsebool(attr.get_item_name(value))
        elif attr.name == "sound":
            self.sound_on = parsebool(attr.get_item_name(value))
        elif attr.name == "quiet":
            self.quiet_on = parsebool(attr.get_item_name(value))
        elif attr.name == "turbo":
            self.turbo_on = parsebool(attr.get_item_name(value))
        elif attr.name == "health":
            self.health_on = parsebool(attr.get_item_name(value))
        elif attr.name == "comfort":
            self.comfort_on = parsebool(attr.get_item_name(value))
        elif attr.name == "cleaning":
            self.cleaning_on = parsebool(attr.get_item_name(value))
        elif attr.name == "antifreeze":
            self.antifreeze_on = parsebool(attr.get_item_name(value))
        elif attr.name == "autohumidity":
            self.autohumidity_on = parsebool(attr.get_item_name(value))
        elif attr.name == "eco_sensor":
            self.eco_sensor = attr.get_item_name(value)

    def _load_config_from_attributes(self, data: dict) -> None:
        self._config = CFG.HaierACConfig(self.device_model, self.hass.config.path(C.DOMAIN))
        attributes = data.setdefault("attributes", [])
        sensors = data.setdefault("sensors", {}).get("items", [])
        sensor_curr_temp = next(filter(lambda i: (
            isinstance(i, dict)
            and isinstance(i.get("value"), dict)
            and i.get("value", {}).get("description") == "indoorTemperature"
        ), sensors), {}).get("value", {}).get("name")
        attrs = list(sorted(map(lambda x: CFG.Attribute(x), attributes), key=lambda x: x.code))
        for attr in attrs:
            if attr.name == "current_temperature" and str(attr.code) != sensor_curr_temp:
                continue
            self.config.attrs.append(attr)
        self.config.merge_attributes()
        for attr in self.config.attrs:
            self._set_attribute_value(str(attr.code), attr.current)
            if attr.name == "target_temperature":
                self.min_temperature = float(attr.range.min_value)
                self.max_temperature = float(attr.range.max_value)
            _LOGGER.debug(f"{self.device_name}: {attr}")
        self.constraint.extend(data.setdefault("constraint", []))

    def _get_status(self, data: dict) -> dict:
        data = super()._get_status(data)
        if self.swing_horizontal_mode is None:
            self.swing_horizontal_mode = SWING_OFF
        if self.swing_mode is None:
            self.swing_mode = SWING_OFF
        if self.preset_mode is None:
            self.preset_mode = PRESET_NONE
        self.write_ha_state()
        return data

    def init_if_needed(self) -> None:
        if not self._inited and next(filter(
            lambda a: (not a.name.startswith("preset_mode_") and a.current is None),
            self.config.attrs
        ), None) is not None:
            self.set_temperature(self.target_temperature)
        self._inited = True

    def get_commands(self, name: str, value: str | bool) -> list[dict]:
        if name != "preset_mode":
            return super().get_commands(name, value)
        func = getattr(self, f"get_preset_mode_{value}", None)
        if func is not None:
            return func()
        return self.get_preset_mode_command(value)

    def get_preset_mode_none(self) -> list[dict]:
        if custom := self.config.get_command_by_name('preset_mode_none'):
            return custom
        return [{
            "commandName": str(attr.code),
            "value": attr.get_item_code("off", "0"),
        } for attr in filter(
            lambda a: a.name.startswith("preset_mode"),
            self.config.attrs
        )]

    def get_preset_mode_command(self, mode: str) -> list[dict]:
        if custom := self.config.get_command_by_name(f'preset_mode_{mode}'):
            return custom
        attr = self.config.get_attr_by_name(f"preset_mode_{mode}")
        return self.constraint.apply([{
            "commandName": str(attr.code),
            "value": attr.get_item_code("on", "1")
        }] if attr else [])

    def get_supported_features(self) -> ClimateEntityFeature:
        value = (
            ClimateEntityFeature.TARGET_TEMPERATURE |
            ClimateEntityFeature.TURN_OFF |
            ClimateEntityFeature.TURN_ON |
            ClimateEntityFeature.FAN_MODE
        )
        if self.config['swing_horizontal_mode'] is not None:
            value = value | ClimateEntityFeature.SWING_HORIZONTAL_MODE
        if self.config['swing_mode'] is not None:
            value = value | ClimateEntityFeature.SWING_MODE
        if self.config.preset_mode is True:
            value = value | ClimateEntityFeature.PRESET_MODE
        return ClimateEntityFeature(value)

    def get_hvac_modes(self) -> list[HVACMode]:
        modes = []
        for mode in self.config.get_values('mode'):
            try:
                modes.append(HVACMode(mode))
            except ValueError:
                pass
        return modes + [HVACMode.OFF]

    def get_fan_modes(self) -> list[str]:
        return self.config.get_values('fan_mode')

    def get_swing_horizontal_modes(self) -> list[str]:
        return self.config.get_values('swing_horizontal_mode')

    def get_swing_modes(self) -> list[str]:
        return self.config.get_values('swing_mode')

    def get_preset_modes(self) -> list[str]:
        return ["none"] + self.config.get_preset_modes()

    def get_eco_sensor_options(self) -> list[str]:
        return self.config.get_values('eco_sensor')

    def set_temperature(self, value: float) -> None:
        self._send_commands([
            {
                "commandName": self.config['target_temperature'],
                "value": str(value)
            }
        ])
        self.target_temperature = value

    def switch_on(self, value: str = None) -> None:
        value = value or self.mode or HVACMode.AUTO
        self._send_commands([
            *(self.get_commands("status", "on") if not self.status else []),
            *self.get_commands("mode", value),
        ])
        self.status = 1
        self.mode = value

    def switch_off(self) -> None:
        self._send_commands([
            *self.get_commands("status", "off"),
        ])
        self.status = 0

    def set_fan_mode(self, value: str) -> None:
        if commands := self.get_commands("fan_mode", value):
            self._send_commands(commands)
            self.fan_mode = value

    def set_swing_horizontal_mode(self, value: str) -> None:
        if commands := self.get_commands("swing_horizontal_mode", value):
            self._send_commands(commands)
            self.swing_horizontal_mode = value

    def set_swing_mode(self, value: str) -> None:
        if commands := self.get_commands("swing_mode", value):
            self._send_commands(commands)
            self.swing_mode = value

    def set_preset_mode(self, value: str) -> None:
        if commands := self.get_commands("preset_mode", value):
            self._send_commands(commands)
            self.preset_mode = value

    def set_light_on(self, value: bool) -> None:
        if commands := self.get_commands("light", value):
            self._send_commands(commands)
            self.light_on = value

    def set_sound_on(self, value: bool) -> None:
        if commands := self.get_commands("sound", value):
            self._send_commands(commands)
            self.sound_on = value

    def set_quiet_on(self, value: bool) -> None:
        if commands := self.get_commands("quiet", value):
            self._send_commands(commands)
            self.quiet_on = value

    def set_health_on(self, value: bool) -> None:
        if commands := self.get_commands("health", value):
            self._send_commands(commands)
            self.health_on = value

    def set_turbo_on(self, value: bool) -> None:
        if commands := self.get_commands("turbo", value):
            self._send_commands(commands)
            self.turbo_on = value

    def set_comfort_on(self, value: bool) -> None:
        if commands := self.get_commands("comfort", value):
            self._send_commands(commands)
            self.comfort_on = value

    def set_cleaning_on(self, value: bool) -> None:
        if commands := self.get_commands("cleaning", value):
            self._send_commands(commands)
            self.cleaning_on = value

    def set_antifreeze_on(self, value: bool) -> None:
        if commands := self.get_commands("antifreeze", value):
            self._send_commands(commands)
            self.antifreeze_on = value

    def set_autohumidity_on(self, value: bool) -> None:
        if commands := self.get_commands("autohumidity", value):
            self._send_commands(commands)
            self.autohumidity_on = value

    def set_eco_sensor(self, value: str) -> None:
        if commands := self.get_commands("eco_sensor", value):
            self._send_commands(commands)
            self.eco_sensor = value

    def create_entities_climate(self) -> list:
        from . import climate
        return [climate.HaierACEntity(self)]
    
    def create_entities_switch(self) -> list:
        from . import switch
        entities = []
        if self.config['light'] is not None:
            entities.append(switch.HaierACLightSwitch(self))
        if self.config['sound'] is not None:
            entities.append(switch.HaierACSoundSwitch(self))
        if self.config['quiet'] is not None:
            entities.append(switch.HaierACQuietSwitch(self))
        if self.config['turbo'] is not None:
            entities.append(switch.HaierACTurboSwitch(self))
        if self.config['health'] is not None:
            entities.append(switch.HaierACHealthSwitch(self))
        if self.config['comfort'] is not None:
            entities.append(switch.HaierACComfortSwitch(self))
        if self.config['cleaning'] is not None:
            entities.append(switch.HaierACCleaningSwitch(self))
        if self.config['antifreeze'] is not None:
            entities.append(switch.HaierACAntiFreezeSwitch(self))
        if self.config['autohumidity'] is not None:
            entities.append(switch.HaierACAutoHumiditySwitch(self))
        return entities

    def create_entities_select(self) -> list:
        from . import select
        entities = []
        if self.config['eco_sensor'] is not None:
            entities.append(select.HaierACEcoSensorSelect(self))
        return entities


class HaierREF(HaierDevice):

    def __init__(
        self,
        backend_data: dict = None,
        **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self.current_fridge_temperature = 0
        self.current_freezer_temperature = 0
        self.current_temperature = 0
        self.fridge_mode = None
        self.freezer_mode = None
        self.my_zone = None
        self.super_cooling = False
        self.super_freeze = False
        self.vacation_mode = False
        self.door_open = False
        self._get_status(backend_data)

    @property
    def config(self) -> CFG.HaierREFConfig:
        return self._config

    def to_dict(self) -> dict:
        data = super().to_dict()
        data.update({
            "current_fridge_temperature": self.current_fridge_temperature,
            "current_freezer_temperature": self.current_freezer_temperature,
            "current_temperature": self.current_temperature,
            "fridge_mode": self.fridge_mode,
            "freezer_mode": self.freezer_mode,
            "my_zone": self.my_zone,
            "super_cooling": self.super_cooling,
            "super_freeze": self.super_freeze,
            "vacation_mode": self.vacation_mode,
            "door_open": self.door_open,
        })
        return data

    def _load_config_from_attributes(self, data: dict) -> None:
        self._config = CFG.HaierREFConfig(self.device_model, self.hass.config.path(C.DOMAIN))
        attributes = data.setdefault("attributes", [])
        attrs = list(sorted(map(lambda x: CFG.Attribute(x), attributes), key=lambda x: x.code))
        for attr in attrs:
            self.config.attrs.append(attr)
        self.config.merge_attributes()
        for attr in self.config.attrs:
            self._set_attribute_value(str(attr.code), attr.current)
            _LOGGER.debug(f"{self.device_name}: {attr}")

    def _set_attribute_value(self, code: str, value: str) -> None:
        attr = self.config.get_attr_by_code(code)
        if not (attr and value is not None):
            return
        elif attr.name == "current_fridge_temperature":
            self.current_fridge_temperature = float(value)
        elif attr.name == "current_freezer_temperature":
            self.current_freezer_temperature = float(value)
        elif attr.name == "current_temperature":
            self.current_temperature = float(value)
        elif attr.name == "fridge_mode":
            self.fridge_mode = attr.get_item_name(value)
        elif attr.name == "freezer_mode":
            self.freezer_mode = attr.get_item_name(value)
        elif attr.name == "my_zone":
            self.my_zone = attr.get_item_name(value)
        elif attr.name == "super_cooling":
            self.super_cooling = parsebool(attr.get_item_name(value))
        elif attr.name == "super_freeze":
            self.super_freeze = parsebool(attr.get_item_name(value))
        elif attr.name == "vacation_mode":
            self.vacation_mode = parsebool(attr.get_item_name(value))
        elif attr.name == "door_open":
            self.door_open = parsebool(attr.get_item_name(value))

    def get_fridge_mode_options(self) -> list[str]:
        return self.config.get_values('fridge_mode')

    def get_freezer_mode_options(self) -> list[str]:
        return self.config.get_values('freezer_mode')

    def get_my_zone_options(self) -> list[str]:
        return self.config.get_values('my_zone')

    def set_super_cooling(self, value: bool) -> None:
        if commands := self.get_commands("super_cooling", value):
            self._send_single_command(commands[0])
            self.super_cooling = value

    def set_super_freeze(self, value: bool) -> None:
        if commands := self.get_commands("super_freeze", value):
            self._send_single_command(commands[0])
            self.super_freeze = value

    def set_vacation_mode(self, value: bool) -> None:
        if commands := self.get_commands("vacation_mode", value):
            self._send_single_command(commands[0])
            self.vacation_mode = value

    def set_fridge_mode(self, value: str) -> None:
        if commands := self.get_commands("fridge_mode", value):
            self._send_single_command(commands[0])
            self.fridge_mode = value

    def set_freezer_mode(self, value: str) -> None:
        if commands := self.get_commands("freezer_mode", value):
            self._send_single_command(commands[0])
            self.freezer_mode = value

    def set_my_zone(self, value: str) -> None:
        if commands := self.get_commands("my_zone", value):
            self._send_single_command(commands[0])
            self.my_zone = value

    def create_entities_switch(self) -> list:
        from . import switch
        entities = []
        if getattr(self.config, '__class__', None).__name__ == 'HaierREFConfig':
            if self.config['super_cooling'] is not None:
                entities.append(switch.HaierREFSuperCoolingSwitch(self))
            if self.config['super_freeze'] is not None:
                entities.append(switch.HaierREFSuperFreezeSwitch(self))
            if self.config['vacation_mode'] is not None:
                entities.append(switch.HaierREFVacationSwitch(self))
            return entities
        # WM
        if self.config['steam'] is not None:
            entities.append(switch.HaierWMSteamSwitch(self))
        return entities

    def create_entities_select(self) -> list:
        from . import select
        entities = []
        if self.config['fridge_mode'] is not None:
            entities.append(select.HaierREFFridgeModeSelect(self))
        if self.config['freezer_mode'] is not None:
            entities.append(select.HaierREFFreezerModeSelect(self))
        if self.config['my_zone'] is not None:
            entities.append(select.HaierREFMyZoneSelect(self))
        return entities

    def create_entities_sensor(self) -> list:
        from . import sensor
        entities = []
        if self.config['current_temperature'] is not None:
            entities.append(sensor.HaierREFTemperatureSensor(self))
        if self.config['current_fridge_temperature'] is not None:
            entities.append(sensor.HaierREFFridgeTemperatureSensor(self))
        if self.config['current_freezer_temperature'] is not None:
            entities.append(sensor.HaierREFFreezerTemperatureSensor(self))
        if self.config['fridge_mode'] is not None:
            entities.append(sensor.HaierREFFridgeModeSensor(self))
        if self.config['freezer_mode'] is not None:
            entities.append(sensor.HaierREFFreezerModeSensor(self))
        return entities

    def create_entities_binary_sensor(self) -> list:
        from . import binary_sensor
        entities = []
        if self.config['super_cooling'] is not None:
            entities.append(binary_sensor.HaierREFSuperCoolingSensor(self))
        if self.config['super_freeze'] is not None:
            entities.append(binary_sensor.HaierREFSuperFreezeSensor(self))
        if self.config['vacation_mode'] is not None:
            entities.append(binary_sensor.HaierREFVacationSensor(self))
        if self.config['door_open'] is not None:
            entities.append(binary_sensor.HaierREFDoorSensor(self))
        return entities


class HaierWM(HaierDevice):

    def __init__(
        self,
        backend_data: dict = None,
        **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self.program = None
        self.temperature = None
        self.spin_speed = None
        self.remaining_minutes = 0
        self.remaining_hours = 0
        self.status_text = None
        self._get_status(backend_data)

    @property
    def config(self) -> CFG.HaierWMConfig:
        return self._config

    def _get_status(self, data: dict) -> dict:
        raw = data or {}
        control = raw.get("smartDeviceControl", {})
        self._status_data = raw
        info = control.get("info", {})
        self.device_serial = info.get("serialNumber", self.device_serial)
        device_model = info.get("model", "WM")
        device_model = device_model.replace('-', '').replace('/', '')[:11]
        self.device_model = device_model
        self.available = raw.get("status") or control.get("status", "ONLINE")
        settings = control.get("settings", {})
        self.device_name = ((settings.get("name") or {}).get("name")) or self.device_name
        self.sw_version = ((settings.get("firmware") or {}).get("value")) or self.sw_version
        # read config and current values
        self._load_config_from_attributes(raw)
        self.write_ha_state()
        return raw

    def _load_config_from_attributes(self, data: dict) -> None:
        self._config = CFG.HaierWMConfig(self.device_model, self.hass.config.path(C.DOMAIN))
        control = data.get("smartDeviceControl", {})
        attributes = control.get("attributes", [])
        attrs = list(sorted(map(lambda x: CFG.Attribute(x), attributes), key=lambda x: x.code))
        for attr in attrs:
            self.config.attrs.append(attr)
        self.config.merge_attributes()
        # Обновляем список программ человекочитаемыми именами из allProgram/businessAttributes
        try:
            self._augment_program_names(data)
        except Exception as e:
            _LOGGER.debug(f"WM program names mapping skipped: {e}")
        for attr in self.config.attrs:
            self._set_attribute_value(str(attr.code), attr.current)
        # Расширяем constraint из control.constraint
        self.constraint.extend(control.get("constraint", []))
        # Лог текущего списка программ для отладки
        prog = self.config.get_attr_by_name("program")
        if prog:
            try:
                names = [getattr(i, "name", None) or i.get("name") for i in prog.list]
                _LOGGER.debug(f"WM program options: {names}")
            except Exception:
                pass

    def _set_attribute_value(self, code: str, value: str) -> None:
        attr = self.config.get_attr_by_code(code)
        if not (attr and value is not None):
            return
        elif attr.name == "program":
            self.program = attr.get_item_name(value)
        elif attr.name == "temperature":
            self.temperature = attr.get_item_name(value) or value
        elif attr.name == "spin_speed":
            self.spin_speed = attr.get_item_name(value) or value
        elif attr.name == "remaining_minutes":
            try:
                self.remaining_minutes = int(value)
            except Exception:
                pass
        elif attr.name == "remaining_hours":
            try:
                self.remaining_hours = int(value)
            except Exception:
                pass
        elif attr.name == "program_duration":
            try:
                # поле 90 приходит в секундах/минутах в разных моделях, оставим как есть
                self.program_duration = int(value)
            except Exception:
                pass
        elif attr.name == "steam":
            # keep current via item name mapping
            pass
        # compute status heuristically
        total = self.remaining_total_minutes
        self.status_text = "running" if total > 0 else "idle"

    def _augment_program_names(self, data: dict) -> None:
        # Собираем отображение washType -> русское имя из раздела allProgram (preview.name)
        wash_to_human: dict[str, str] = {}

        def walk_collect(node):
            if isinstance(node, dict):
                programs = node.get("programs")
                if isinstance(programs, list):
                    for p in programs:
                        link = (((p.get("programConfig") or {}).get("link") or {}).get("name"))
                        human = ((p.get("preview") or {}).get("name"))
                        if link and human:
                            wash_to_human[str(link)] = str(human)
                for v in node.values():
                    walk_collect(v)
            elif isinstance(node, list):
                for v in node:
                    walk_collect(v)

        walk_collect(data)

        # Собираем код программы (attr name "0") -> русское имя
        code_to_human: dict[str, str] = {}
        for item in data.get("businessAttributes", []) or []:
            wash_name = str(item.get("name") or "")
            attrs = (((item.get("commandParameters") or {}).get("attrNameList") or []))
            program_attr = next((a for a in attrs if str(a.get("name")) == "0"), None)
            program_code = str(program_attr.get("defaultValue")) if program_attr else None
            if not program_code or program_code in ("", "None", "null"):
                continue
            human_name = wash_to_human.get(wash_name)
            if not human_name and wash_name == "refresh":
                human_name = "Освежить"
            if human_name:
                code_to_human[program_code] = human_name

        if not code_to_human:
            return

        # Обновляем список значений у атрибута program
        attr = self.config.get_attr_by_name("program")
        if not attr:
            return
        # Сохраняем исходный порядок из attr.list, только подменяем имена
        if attr.list:
            for item in attr.list:
                code = str(item.get("data"))
                if code in code_to_human:
                    human = code_to_human[code]
                    # обновляем как внутренние поля dict, так и атрибут Item.name
                    item["name"] = human
                    item["attrname"] = human
                    try:
                        setattr(item, "name", human)
                    except Exception:
                        pass
        else:
            new_items = []
            for code, name in sorted(code_to_human.items(), key=lambda x: x[1]):
                new_items.append({
                    "data": str(code),
                    "name": str(name),
                    "attrname": str(name),
                })
            attr.list = new_items

    def start_program(self) -> None:
        # просто отправляем текущее состояние как  command (commandName self.config.command_name)
        # минимально — меняем статус/режим через рабочий набор атрибутов, если требуется
        commands = []
        program_attr = self.config.get_attr_by_name("program")
        if program_attr and program_attr.current is not None:
            commands.append({"commandName": str(program_attr.code), "value": str(program_attr.current)})
        temp_attr = self.config.get_attr_by_name("temperature")
        if temp_attr and temp_attr.current is not None:
            commands.append({"commandName": str(temp_attr.code), "value": str(temp_attr.current)})
        spin_attr = self.config.get_attr_by_name("spin_speed")
        if spin_attr and spin_attr.current is not None:
            commands.append({"commandName": str(spin_attr.code), "value": str(spin_attr.current)})
        rinse_attr = self.config.get_attr_by_name("rinse_count")
        if rinse_attr and rinse_attr.current is not None:
            commands.append({"commandName": str(rinse_attr.code), "value": str(rinse_attr.current)})
        delay_attr = self.config.get_attr_by_name("delayed_hours")
        if delay_attr and delay_attr.current is not None:
            commands.append({"commandName": str(delay_attr.code), "value": str(delay_attr.current)})
        if commands:
            self._send_commands(commands)

    def pause_program(self) -> None:
        # У некоторых моделей пауза реализуется командой 15/stopCurrentAlarm/и т.п.
        command = self.config.get_command_by_name('pause')
        if command:
            self._send_single_command(command[0])

    def cancel_program(self) -> None:
        command = self.config.get_command_by_name('cancel')
        if command:
            self._send_single_command(command[0])

    # options
    def get_program_options(self) -> list[str]:
        return self.config.get_values('program')

    def get_temperature_options(self) -> list[str]:
        return self.config.get_values('temperature')

    def get_spin_speed_options(self) -> list[str]:
        return self.config.get_values('spin_speed')

    def get_rinse_count_options(self) -> list[str]:
        return self.config.get_values('rinse_count')

    def set_delayed_hours(self, value: str) -> None:
        # value ожидается как строка вида '0', '0.5', '1', ..., переведем в минуты/коды если потребуется
        attr = self.config.get_attr_by_name('delayed_hours')
        if attr is None:
            return
        minutes = value
        if commands := self.constraint.apply([{
            "commandName": str(attr.code),
            "value": str(minutes),
        }]):
            self._send_commands(commands)

    # setters
    def set_program(self, value: str) -> None:
        if commands := self.get_commands("program", value):
            self._send_single_command(commands[0])
            self.program = value

    def set_temperature(self, value: str) -> None:
        if commands := self.get_commands("temperature", value):
            self._send_single_command(commands[0])
            self.temperature = value

    def set_spin_speed(self, value: str) -> None:
        if commands := self.get_commands("spin_speed", value):
            self._send_single_command(commands[0])
            self.spin_speed = value

    def set_rinse_count(self, value: str) -> None:
        if commands := self.get_commands("rinse_count", value):
            self._send_single_command(commands[0])

    def create_entities_select(self) -> list:
        from . import select
        entities = []
        if self.config['program'] is not None:
            entities.append(select.HaierWMProgramSelect(self))
        if self.config['temperature'] is not None:
            entities.append(select.HaierWMTemperatureSelect(self))
        if self.config['spin_speed'] is not None:
            entities.append(select.HaierWMSpinSpeedSelect(self))
        if self.config['rinse_count'] is not None:
            entities.append(select.HaierWMRinseCountSelect(self))
        if self.config['delayed_hours'] is not None:
            entities.append(select.HaierWMDelayedFinishSelect(self))
        return entities

    def create_entities_sensor(self) -> list:
        from . import sensor
        entities = []
        if self.config['remaining_minutes'] is not None:
            entities.append(sensor.HaierWMRemainingTimeSensor(self))
        # статус
        entities.append(sensor.HaierWMStatusSensor(self))
        if self.config['program_duration'] is not None:
            entities.append(sensor.HaierWMProgramDurationSensor(self))
        return entities

    def create_entities_button(self) -> list:
        from . import button
        return [
            button.HaierWMStartButton(self),
            button.HaierWMPauseButton(self),
            button.HaierWMCancelButton(self),
        ]

    @property
    def remaining_total_minutes(self) -> int:
        try:
            return max(0, int(self.remaining_hours) * 60 + int(self.remaining_minutes))
        except Exception:
            return int(self.remaining_minutes or 0)

def parsebool(value) -> bool:
    if value in ("on", 1, True, "true", "enable", "1"):
        return True
    return False
