from scripts.Nokia_SAR import Script as NokiaSARScript
from scripts.nokia_raw_transcript import RawNokiaTranscriptMixin


class Script(RawNokiaTranscriptMixin, NokiaSARScript):
    RAW_DEVICE_TYPE = "Nokia 7705 SAR"