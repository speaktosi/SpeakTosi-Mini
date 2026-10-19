from faster_whisper import WhisperModel
import sys

model = WhisperModel("large-v3", device="cpu", compute_type="int8")
segments, info = model.transcribe(sys.argv[1], language="zh", vad_filter=True)
print(f"detected_lang={info.language} prob={info.language_probability:.2f}")
for s in segments:
    print(f"[{s.start:6.2f} -> {s.end:6.2f}] {s.text}")
