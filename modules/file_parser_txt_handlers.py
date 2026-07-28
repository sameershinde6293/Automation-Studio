"""Compatibility export combining TXT handler mixins."""

from modules.file_parser_txt_header import TxtHeaderVoiceMixin
from modules.file_parser_txt_scene import TxtSceneDialogueMixin


class TxtLineHandlerMixin(TxtSceneDialogueMixin, TxtHeaderVoiceMixin):
    """Combined TXT line-handler mixin."""

    pass
