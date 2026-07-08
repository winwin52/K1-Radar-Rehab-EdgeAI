import sounddevice, soundfile, numpy
print(f"sounddevice {sounddevice.__version__}")
print(f"soundfile   {soundfile.__version__}")
print(f"numpy       {numpy.__version__}")
print()
print("Output devices:")
for i, d in enumerate(sounddevice.query_devices()):
    if d['max_output_channels'] > 0:
        print(f"  [{i}] {d['name']}  ({d['max_output_channels']} ch, default sr {d['default_samplerate']:.0f})")
