"""Safe, stage-aware failures for one bounded SSH telemetry attempt.

Only fixed messages leave this module. A remote probe marker establishes that
SSH reached the asset; subsequent command failures must not mark it offline.
"""
from __future__ import annotations

from fl_dashboard.vendor.jms_top.scan import reached

_MAX_TEXT = 128 * 1024
_HOST_KEY = ("host key verification failed", "remote host identification has changed")
_AUTHENTICATION = ("permission denied", "authentication failed", "too many authentication failures",
                   "no supported authentication methods available")
_TRANSPORT = ("connection reset", "connection closed", "connection timed out", "operation timed out",
              "connection refused", "connection aborted", "network is unreachable", "no route to host",
              "kex_exchange_identification", "ssh_exchange_identification", "banner exchange",
              "temporary failure in name resolution")


def _text(value):
    if isinstance(value, bytes):
        return value[:_MAX_TEXT].decode("utf-8", "replace")
    return value[:_MAX_TEXT] if isinstance(value, str) else ""


def _failure(message, *, reachable, retryable, kind):
    return {"message": message, "reachable": reachable, "retryable": retryable, "kind": kind}


def classify_failure(returncode, stdout, stderr):
    """Classify an attempt without exposing remote output or inferring availability.

    A zero exit with a valid start marker belongs to the telemetry parser, which
    validates the completion marker and metrics. Other zero exits do not prove a
    working connection: gateways may return success while rejecting an asset.
    """
    output = _text(stdout)
    connected = reached(output)
    if returncode == 0:
        if connected:
            return None
        return _failure("SSH 未返回有效的设备采集响应，暂时无法确认连接状态。",
                        reachable=None, retryable=True, kind="protocol")
    if connected:
        if returncode == 255:
            return _failure("已连接设备，但采集过程中连接中断，请稍后重试。",
                            reachable=True, retryable=True, kind="transport")
        return _failure("已连接设备，但采集命令执行失败，请检查远端采集环境。",
                        reachable=True, retryable=False, kind="remote_command")
    # OpenSSH reserves 255 for its own failures; other codes are returned by the
    # remote command. A command's permission error is not an SSH login failure.
    if returncode != 255:
        return _failure("设备采集命令未正常完成，暂时无法确认连接状态。",
                        reachable=None, retryable=False, kind="remote_command")
    diagnostic = (output + "\n" + _text(stderr)).lower()
    if any(pattern in diagnostic for pattern in _HOST_KEY):
        return _failure("SSH 主机密钥校验失败，请在本机终端核对并确认。",
                        reachable=False, retryable=False, kind="host_key")
    if any(pattern in diagnostic for pattern in _AUTHENTICATION):
        return _failure("SSH 认证失败，请检查已保存的连接与资产账号授权。",
                        reachable=False, retryable=False, kind="authentication")
    if "timed out" in diagnostic:
        return _failure("SSH 入口连接超时，尚未进入设备采集，请稍后重试。",
                        reachable=False, retryable=True, kind="transport")
    if any(pattern in diagnostic for pattern in _TRANSPORT):
        return _failure("SSH 连接暂时中断或不可达，请稍后重试。",
                        reachable=False, retryable=True, kind="transport")
    return _failure("设备采集连接异常，暂时无法确认连接状态。",
                    reachable=None, retryable=True, kind="connection")
