import wave, struct, math, sys

def spec(f):
    w = wave.open(f)
    n = w.getnframes(); sr = w.getframerate()
    samples = struct.unpack('<%dh' % n, w.readframes(n))
    win = sr
    best, besti = 0, 0
    for i in range(0, n - win, win):
        e = sum(s * s for s in samples[i:i + win])
        if e > best:
            best, besti = e, i
    seg = samples[besti:besti + win]
    step = max(1, len(seg) // 4096)
    pts = [seg[i] for i in range(0, len(seg), step)][:2048]
    N = len(pts)
    mags = []
    for k in range(0, 200):
        re = im = 0.0
        for i, v in enumerate(pts):
            a = 2 * math.pi * k * i / N
            re += v * math.cos(a)
            im += v * math.sin(a)
        mags.append(math.sqrt(re * re + im * im) / N)
    bands = [sum(mags[k * 10:k * 10 + 10]) for k in range(12)]
    tot = sum(bands) + 1e-9
    labels = ['0-2.4k','2.4-4.8k','4.8-7.2k','7.2-9.6k','9.6-12k','12-14.4k','14.4-16.8k','16.8-19.2k','19.2-21.6k','21.6-24k']
    print(f, f'peak@{besti/sr:.1f}s={max(abs(s) for s in seg)}')
    print('  ' + ' '.join(f'{100*b/tot:5.1f}%' for b in bands))

for f in sys.argv[1:]:
    spec(f)
