import base64

import pytest
from ipyplotly.basevalidators import ImageUriValidator
import numpy as np
from PIL import Image


# Fixtures
# --------
@pytest.fixture()
def validator():
    return ImageUriValidator('prop', 'parent')


# Tests
# -----
# ### Acceptance ###
@pytest.mark.parametrize('val', [
    'http://somewhere.com/images/image12.png',
    'data:image/png;base64,iVBORw0KGgoAAAANSU',
])
def test_validator_acceptance(val, validator: ImageUriValidator):
    assert validator.validate_coerce(val) == val


# ### Coercion from PIL Image ###
def test_validator_coercion_PIL(validator: ImageUriValidator):
    # Single pixel black png (http://png-pixel.com/)

    img_path = 'test/resources/1x1-black.png'
    with open(img_path, 'rb') as f:
        hex_bytes = base64.b64encode(f.read()).decode('ascii')
        expected_uri = 'data:image/png;base64,' + hex_bytes

    img = Image.open(img_path)
    coerce_val = validator.validate_coerce(img)
    assert coerce_val == expected_uri