"""启动 audiocpp TTS server（带 SpeakTosi Demo Voice 声音克隆 preset），detached 后台运行。"""
import json, os, subprocess, sys, time, urllib.request

ROOT = r'D:\AI-ModeLs\audiocpp-portable'
PORT = 18080
CFG = r'C:\Users\owenlee\hermes_scripts\aiteacher\tts_demo_voice.json'
EXE = os.path.join(ROOT, 'gpu', 'audiocpp_server.exe')

REF_WAV = os.path.join(ROOT, 'webui', 'voice', 'demo_voice_ref.wav')
REF_TEXT = ("Welcome to the Phrase Master Intermediate Challenge. I'm your examiner, SpeakTosi Demo Voice, "
            "and I'm truly delighted to embark on this intense English breakthrough journey alongside you all. "
            "This round features 50 questions that will test your mastery of high-frequency English phrases. "
            "Are you ready? Choose carefully, as many options may seem plausible at first glance, "
            "but only one perfectly fits the context. Good luck!")

preset = {"voice_ref": REF_WAV, "reference_text": REF_TEXT}
voices = ["default","alloy","ash","ballad","coral","echo","fable","onyx","nova","sage","shimmer","verse"]

model_id = sys.argv[1] if len(sys.argv) > 1 else "qwen3-tts"
CAT = json.load(open(os.path.join(ROOT, 'webui', 'configs', 'models_catalog.json'), encoding='utf-8'))
entry = next(m for m in CAT['models'] if m['id'] == model_id)
mpath = os.path.join(ROOT, entry['path'])
assert os.path.isdir(mpath), 'model dir missing: ' + mpath
assert os.path.isfile(REF_WAV), 'ref wav missing: ' + REF_WAV

cfgd = {
    "host": "127.0.0.1", "port": PORT, "backend": "cuda", "device": 0, "threads": 8,
    "models": [{
        "id": model_id, "family": entry['family'], "path": mpath,
        "task": entry['task'], "mode": "offline",
        "voice_presets": {v: dict(preset) for v in voices},
        "default_voice_preset": "default",
    }],
}
json.dump(cfgd, open(CFG, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
print('config written:', CFG)
print('model:', model_id, '|', entry['family'], '|', mpath)
print('voice_ref:', REF_WAV)

DETACHED = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
log = open(r'C:\Users\owenlee\hermes_scripts\aiteacher\tts_server.log', 'wb')
p = subprocess.Popen([EXE, '--config', CFG, '--host', '127.0.0.1', '--port', str(PORT), '--device', '0'],
                     cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, creationflags=DETACHED,
                     close_fds=True)
print('server pid:', p.pid)

for i in range(36):
    time.sleep(5)
    try:
        r = urllib.request.urlopen('http://127.0.0.1:%d/health' % PORT, timeout=3)
        print('READY after %ds: %s' % ((i+1)*5, r.read().decode()[:200]))
        break
    except Exception:
        if i % 3 == 0:
            print('  waiting... %ds' % ((i+1)*5))
else:
    print('TIMEOUT — check tts_server.log')
    print(open(r'C:\Users\owenlee\hermes_scripts\aiteacher\tts_server.log', encoding='utf-8', errors='replace').read()[-2000:])
