# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd. and/or its affiliates.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys
from types import ModuleType
from types import SimpleNamespace

from veadk.integrations.ve_tls import ve_tls
from veadk.tracing.telemetry.exporters import tls_exporter


def _clear_volcengine_env(monkeypatch):
    for name in (
        "VOLCENGINE_ACCESS_KEY",
        "VOLCENGINE_SECRET_KEY",
        "VOLCENGINE_SESSION_TOKEN",
        "VOLC_SESSIONTOKEN",
        "BYTEPLUS_ACCESS_KEY",
        "BYTEPLUS_SECRET_KEY",
        "CLOUD_PROVIDER",
    ):
        monkeypatch.delenv(name, raising=False)


def _fake_iam_credential():
    return SimpleNamespace(
        access_key_id="iam-ak",
        secret_access_key="iam-sk",
        session_token="iam-sts",
    )


def test_tls_credentials_fall_back_to_vefaas_iam(monkeypatch):
    _clear_volcengine_env(monkeypatch)
    monkeypatch.setattr(ve_tls, "get_credential_from_vefaas_iam", _fake_iam_credential)

    credential = ve_tls.resolve_volcengine_credentials()

    assert credential.access_key_id == "iam-ak"
    assert credential.secret_access_key == "iam-sk"
    assert credential.session_token == "iam-sts"


def test_tls_credentials_use_environment_session_token(monkeypatch):
    _clear_volcengine_env(monkeypatch)
    monkeypatch.setenv("VOLCENGINE_ACCESS_KEY", "env-ak")
    monkeypatch.setenv("VOLCENGINE_SECRET_KEY", "env-sk")
    monkeypatch.setenv("VOLCENGINE_SESSION_TOKEN", "env-sts")

    credential = ve_tls.resolve_volcengine_credentials()

    assert credential.access_key_id == "env-ak"
    assert credential.secret_access_key == "env-sk"
    assert credential.session_token == "env-sts"


def test_ve_tls_passes_iam_session_token_to_tls_service(monkeypatch):
    _clear_volcengine_env(monkeypatch)
    monkeypatch.setattr(ve_tls, "get_credential_from_vefaas_iam", _fake_iam_credential)

    class _FakeCredentials:
        def __init__(self):
            self.session_token = ""

        def set_session_token(self, session_token):
            self.session_token = session_token

    class _FakeTLSService:
        def __init__(self, **kwargs):
            self.init_kwargs = kwargs
            self.service_info = SimpleNamespace(credentials=_FakeCredentials())

    tls_service_module = ModuleType("volcengine.tls.TLSService")
    tls_service_module.TLSService = _FakeTLSService
    monkeypatch.setitem(sys.modules, "volcengine.tls.TLSService", tls_service_module)

    client = ve_tls.VeTLS(region="cn-shanghai")

    assert client.access_key == "iam-ak"
    assert client.secret_key == "iam-sk"
    assert client.session_token == "iam-sts"
    assert client._client.init_kwargs["access_key_id"] == "iam-ak"
    assert client._client.init_kwargs["access_key_secret"] == "iam-sk"
    assert client._client.init_kwargs["security_token"] == "iam-sts"
    assert client._client.service_info.credentials.session_token == "iam-sts"


def test_tls_exporter_config_falls_back_to_vefaas_iam(monkeypatch):
    _clear_volcengine_env(monkeypatch)
    monkeypatch.setattr(
        tls_exporter,
        "resolve_volcengine_credentials",
        lambda **_: _fake_iam_credential(),
    )

    config = tls_exporter.TLSExporterConfig(
        endpoint="http://localhost:4318/v1/traces",
        region="cn-beijing",
        topic_id="topic-id",
    )

    assert config.access_key == "iam-ak"
    assert config.secret_key == "iam-sk"
    assert config.session_token == "iam-sts"


def test_tls_exporter_sends_session_token_header():
    exporter = tls_exporter.TLSExporter(
        config=tls_exporter.TLSExporterConfig(
            endpoint="http://localhost:4318/v1/traces",
            region="cn-beijing",
            topic_id="topic-id",
            access_key="ak",
            secret_key="sk",
            session_token="sts",
        )
    )

    assert exporter.headers["X-Security-Token"] == "sts"
