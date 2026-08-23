from scripts.Nokia_IXR import Script as NokiaIXRScript
from scripts.nokia_raw_transcript import RawNokiaTranscriptMixin


class Script(RawNokiaTranscriptMixin, NokiaIXRScript):
    RAW_DEVICE_TYPE = "Nokia 7250 IXR"