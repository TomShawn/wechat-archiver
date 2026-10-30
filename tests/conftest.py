import io

import pytest
from PIL import Image

from wechat_archiver.config import Config


@pytest.fixture
def config(tmp_path):
    return Config(root=tmp_path, request_interval=0, image_request_interval=0, retries=0)


def png(color=(255, 0, 0), size=(120, 90)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()
