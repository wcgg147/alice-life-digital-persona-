import unittest
from call_audio import Resampler,validate_devices

class CallAudioTests(unittest.TestCase):
    def test_explicit_routes_keep_the_two_native_rates(self):
        devices=[{'hostApi':'Windows WASAPI','name':name,'direction':direction,'id':name,'sampleRate':rate}
                 for name,direction,rate in [('Hi-Fi Cable Output (VB-Audio Hi-Fi Cable)','input',44100),
                    ('CABLE Input (VB-Audio Virtual Cable)','output',48000)]]
        route=validate_devices(devices)
        self.assertEqual((route['inputSampleRate'],route['sampleRate']),(44100,48000))
        with self.assertRaises(RuntimeError):validate_devices(devices+devices)
        with self.assertRaises(RuntimeError):validate_devices([{'hostApi':'Windows WASAPI','name':'麦克风 (iKF-V11 Max)','direction':'input','id':'physical','sampleRate':48000}])

    def test_streaming_resampling_does_not_reset_at_frame_boundaries(self):
        import struct,math
        data=b''.join(struct.pack('<h',int(math.sin(i*.1)*1000)) for i in range(8820))
        whole=Resampler(44100,16000).convert(data)
        stream=Resampler(44100,16000)
        self.assertEqual(whole,b''.join(stream.convert(data[n:n+1764]) for n in range(0,len(data),1764)))
