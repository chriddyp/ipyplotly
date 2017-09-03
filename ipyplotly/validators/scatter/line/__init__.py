import ipyplotly.basevalidators as bv


class ColorValidator(bv.ColorValidator):
    def __init__(self):
        super().__init__(name='color',
                         parent_name='Line')


class WidthValidator(bv.NumberValidator):
    def __init__(self):
        super().__init__(name='width',
                         parent_name='Line',
                         default=2,
                         min_val=0)
