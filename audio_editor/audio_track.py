"""音频轨道数据模型：加载、裁剪、删除区间、导出，都在这里完成。"""
import numpy as np
from pydub import AudioSegment

TARGET_SR = 44100
TARGET_CHANNELS = 2


def export_samples(samples, filepath, sample_rate=TARGET_SR, channels=TARGET_CHANNELS, fmt=None):
    fmt = fmt or (filepath.rsplit(".", 1)[-1].lower() if "." in filepath else "mp3")
    clipped = np.clip(samples, -1.0, 1.0)
    int_samples = (clipped * 32767.0).astype(np.int16)
    seg = AudioSegment(
        int_samples.tobytes(),
        frame_rate=sample_rate,
        sample_width=2,
        channels=channels,
    )
    seg.export(filepath, format=fmt)


class AudioTrack:
    def __init__(self, name=""):
        self.name = name
        self.filepath = None
        self.sample_rate = TARGET_SR
        self.channels = TARGET_CHANNELS
        self.samples = np.zeros((0, TARGET_CHANNELS), dtype=np.float32)  # 取值范围 [-1, 1]
        self._mono_cache = None

    @property
    def num_samples(self):
        return self.samples.shape[0]

    @property
    def duration(self):
        return self.num_samples / self.sample_rate if self.sample_rate else 0.0

    @property
    def loaded(self):
        return self.num_samples > 0

    def load(self, filepath):
        seg = AudioSegment.from_file(filepath)
        seg = seg.set_frame_rate(TARGET_SR).set_channels(TARGET_CHANNELS)
        arr = np.array(seg.get_array_of_samples()).astype(np.float32)
        arr = arr.reshape((-1, TARGET_CHANNELS))
        max_val = float(1 << (8 * seg.sample_width - 1))
        self.samples = arr / max_val
        self.sample_rate = TARGET_SR
        self.channels = TARGET_CHANNELS
        self.filepath = filepath
        self.name = filepath
        self._mono_cache = None

    def set_samples(self, samples):
        self.samples = samples
        self._mono_cache = None

    def time_to_sample(self, t):
        return max(0, min(self.num_samples, int(round(t * self.sample_rate))))

    def silence_range(self, start_t, end_t):
        """把选中的时间段清零（留空），不改变时长，后面内容的时间位置不变。"""
        s = self.time_to_sample(start_t)
        e = self.time_to_sample(end_t)
        if e <= s:
            return
        self.samples[s:e] = 0.0
        self._mono_cache = None

    def crop_to(self, start_t, end_t):
        """裁剪：只保留选区，其余部分丢弃。"""
        s = self.time_to_sample(start_t)
        e = self.time_to_sample(end_t)
        if e <= s:
            return
        self.samples = self.samples[s:e].copy()
        self._mono_cache = None

    def get_mono(self):
        if self._mono_cache is None:
            if self.samples.size == 0:
                self._mono_cache = np.zeros((0,), dtype=np.float32)
            else:
                self._mono_cache = self.samples.mean(axis=1)
        return self._mono_cache

    def envelope(self, start_t, end_t, columns):
        """返回 (mins, maxs)，长度均为 columns，用于绘制波形。"""
        if columns <= 0:
            return np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.float32)
        s = self.time_to_sample(start_t)
        e = self.time_to_sample(end_t)
        mono = self.get_mono()
        if e <= s or mono.size == 0:
            return np.zeros(columns, dtype=np.float32), np.zeros(columns, dtype=np.float32)
        seg = mono[s:e]
        length = len(seg)
        if length >= columns:
            idx = (np.arange(columns) * length / columns).astype(np.int64)
            idx = np.clip(idx, 0, length - 1)
            maxs = np.maximum.reduceat(seg, idx)
            mins = np.minimum.reduceat(seg, idx)
        else:
            src_idx = np.linspace(0, length - 1, columns).astype(np.int64)
            vals = seg[src_idx]
            mins = vals.copy()
            maxs = vals.copy()
        return mins, maxs
