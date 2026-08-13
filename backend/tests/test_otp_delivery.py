from __future__ import annotations

import httpx

from app.config import Settings
from app.services import otp_delivery


def test_msg91_flow_binds_the_case_sensitive_otp_variable(monkeypatch):
    sent: dict[str, object] = {}

    def post(url, **kwargs):
        sent["url"] = url
        sent.update(kwargs)
        return httpx.Response(200, json={"type": "success"})

    monkeypatch.setattr(otp_delivery.httpx, "post", post)
    settings = Settings(
        _env_file=None,
        msg91_auth_key="secret",
        msg91_template_id="template",
        msg91_sender_id="JITRAA",
    )

    otp_delivery.Msg91Sender().send("+919000000099", "123456", settings)

    assert sent["url"] == otp_delivery.MSG91_FLOW_URL
    assert sent["json"] == {
        "template_id": "template",
        "short_url": "0",
        "sender": "JITRAA",
        "recipients": [{"mobiles": "919000000099", "OTP": "123456"}],
    }
