from __future__ import annotations

from email.parser import BytesParser
from email.policy import default as email_policy_default


def parse_multipart_form_data(
    body: bytes,
    content_type: str,
) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    message = BytesParser(policy=email_policy_default).parsebytes(
        b"Content-Type: "
        + content_type.encode("latin-1", errors="replace")
        + b"\r\nMIME-Version: 1.0\r\n\r\n"
        + body
    )
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    if not message.is_multipart():
        return fields, files

    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename is not None:
            files[str(name)] = (str(filename), payload)
            continue
        charset = part.get_content_charset() or "utf-8"
        fields[str(name)] = payload.decode(charset, errors="replace")
    return fields, files
