"""主窗口：工具栏、双轨波形区、播放控制、剪切/裁剪/撤销/导出。"""
import os
import time

import numpy as np
import sounddevice as sd
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (
    QAction, QCheckBox, QFileDialog, QLabel, QMainWindow, QMessageBox,
    QScrollBar, QStyle, QToolBar, QWidget, QHBoxLayout, QVBoxLayout,
)

from .audio_track import AudioTrack, TARGET_SR, export_samples
from .waveform_widget import DualWaveformView, fmt_time

MAX_UNDO = 20
AUDIO_FILTER = "音频文件 (*.mp3 *.wav *.flac *.m4a *.ogg *.aac);;所有文件 (*)"
SCROLL_SCALE = 1000  # 滚动条用整数刻度，这里按毫秒换算


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("双轨音频剪辑器（原版 / 伴奏）")

        self.track1 = AudioTrack("原版/原唱")
        self.track2 = AudioTrack("伴奏")

        self.undo_stack = []
        self.redo_stack = []

        self.mute1 = False
        self.mute2 = False

        self.playing = False
        self._play_wall_start = 0.0
        self._play_pos_start = 0.0
        self._play_end = 0.0

        self.view = DualWaveformView()
        self.view.selectionChanged.connect(self._on_selection_changed)
        self.view.seekRequested.connect(self._on_seek_requested)
        self.view.viewChanged.connect(self._update_status)

        self._build_ui()
        self._build_actions()

        self.play_timer = QTimer(self)
        self.play_timer.timeout.connect(self._on_play_tick)

        self._update_status()
        self._update_actions_enabled()

    # ---------------- UI ----------------
    def _build_ui(self):
        self.statusBar()  # 用于显示工具栏按钮的悬停提示（status tip）

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.view, 1)

        self.scrollbar = QScrollBar(Qt.Horizontal)
        self.scrollbar.setEnabled(False)
        self.scrollbar.valueChanged.connect(self._on_scrollbar_changed)
        layout.addWidget(self.scrollbar)

        bottom = QHBoxLayout()
        self.mute1_box = QCheckBox("静音-原版")
        self.mute2_box = QCheckBox("静音-伴奏")
        self.mute1_box.toggled.connect(self._on_mute1)
        self.mute2_box.toggled.connect(self._on_mute2)
        bottom.addWidget(self.mute1_box)
        bottom.addWidget(self.mute2_box)
        bottom.addStretch(1)
        self.status_label = QLabel("")
        bottom.addWidget(self.status_label)
        layout.addLayout(bottom)

        self.setCentralWidget(central)
        self.resize(1200, 700)

    def _build_actions(self):
        tb = QToolBar("工具栏")
        tb.setMovable(False)
        self.addToolBar(tb)

        style = self.style()

        act_open1 = QAction("打开原版...", self)
        act_open1.triggered.connect(lambda: self._open_track(self.track1, "打开原版/原唱音频"))
        tb.addAction(act_open1)

        act_open2 = QAction("打开伴奏...", self)
        act_open2.triggered.connect(lambda: self._open_track(self.track2, "打开伴奏音频"))
        tb.addAction(act_open2)

        tb.addSeparator()

        self.act_play = QAction(style.standardIcon(QStyle.SP_MediaPlay), "播放", self)
        self.act_play.setShortcut(Qt.Key_Space)
        self.act_play.triggered.connect(self.toggle_play)
        tb.addAction(self.act_play)

        self.act_stop = QAction(style.standardIcon(QStyle.SP_MediaStop), "停止", self)
        self.act_stop.triggered.connect(self.stop_playback)
        tb.addAction(self.act_stop)

        tb.addSeparator()

        self.act_split = QAction("在播放线拆分", self)
        self.act_split.setShortcut(QKeySequence("Ctrl+K"))
        self.act_split.setStatusTip("在播放线位置拆分当前轨道；之后按住 Ctrl 单击某块即可选中整块")
        self.act_split.triggered.connect(self.split_at_playhead)
        tb.addAction(self.act_split)

        tb.addSeparator()

        self.act_delete = QAction("删除选区（留空）", self)
        self.act_delete.setShortcut(QKeySequence(Qt.Key_Delete))
        self.act_delete.setStatusTip("清空选中的时间段，不移动其余内容（Ctrl+单击可选中拆分出的整块）")
        self.act_delete.triggered.connect(self.delete_selection)
        tb.addAction(self.act_delete)

        self.act_trim = QAction("裁剪保留选区", self)
        self.act_trim.setShortcut(QKeySequence("Ctrl+T"))
        self.act_trim.triggered.connect(self.trim_selection)
        tb.addAction(self.act_trim)

        tb.addSeparator()

        self.act_undo = QAction("撤销", self)
        self.act_undo.setShortcut(QKeySequence.Undo)
        self.act_undo.triggered.connect(self.undo)
        tb.addAction(self.act_undo)

        self.act_redo = QAction("重做", self)
        self.act_redo.setShortcut(QKeySequence("Ctrl+Y"))
        self.act_redo.triggered.connect(self.redo)
        tb.addAction(self.act_redo)

        tb.addSeparator()

        act_zoom_in = QAction("放大", self)
        act_zoom_in.setShortcut(QKeySequence("Ctrl+="))
        act_zoom_in.triggered.connect(lambda: self.view.zoom(0.7))
        tb.addAction(act_zoom_in)

        act_zoom_out = QAction("缩小", self)
        act_zoom_out.setShortcut(QKeySequence("Ctrl+-"))
        act_zoom_out.triggered.connect(lambda: self.view.zoom(1 / 0.7))
        tb.addAction(act_zoom_out)

        act_zoom_fit = QAction("适应窗口", self)
        act_zoom_fit.setShortcut(QKeySequence("Ctrl+0"))
        act_zoom_fit.triggered.connect(self.view.zoom_fit)
        tb.addAction(act_zoom_fit)

        tb.addSeparator()

        act_export_mix = QAction("导出合并音轨...", self)
        act_export_mix.setShortcut(QKeySequence("Ctrl+E"))
        act_export_mix.setStatusTip("把原版和伴奏混合成一条音轨，导出为单个文件（遵循当前的静音勾选）")
        act_export_mix.triggered.connect(self.export_mixed)
        tb.addAction(act_export_mix)

    def _update_actions_enabled(self):
        has_both = self.track1.loaded and self.track2.loaded
        has_sel = self.view.selection is not None and (self.view.selection[1] - self.view.selection[0]) > 0
        self.act_play.setEnabled(has_both)
        self.act_stop.setEnabled(has_both)
        self.act_split.setEnabled(has_both)
        self.act_delete.setEnabled(has_both and has_sel)
        self.act_trim.setEnabled(has_both and has_sel)
        self.act_undo.setEnabled(len(self.undo_stack) > 0)
        self.act_redo.setEnabled(len(self.redo_stack) > 0)

    # ---------------- 打开文件 ----------------
    def _open_track(self, track, title):
        path, _ = QFileDialog.getOpenFileName(self, title, "", AUDIO_FILTER)
        if not path:
            return
        try:
            track.load(path)
        except Exception as exc:
            QMessageBox.critical(self, "加载失败", f"无法加载音频文件：\n{path}\n\n{exc}")
            return

        if self.track1.loaded and self.track2.loaded:
            d1, d2 = self.track1.duration, self.track2.duration
            if abs(d1 - d2) > 0.02:
                min_dur = min(d1, d2)
                self.track1.crop_to(0, min_dur)
                self.track2.crop_to(0, min_dur)
                QMessageBox.information(
                    self, "长度已对齐",
                    f"两个音频长度不完全一致（{d1:.2f}s / {d2:.2f}s），已自动裁剪到较短的 {min_dur:.2f}s，保持两轨同步。",
                )

        self.undo_stack.clear()
        self.redo_stack.clear()
        self.view.set_tracks(self.track1, self.track2)
        self._update_status()
        self._update_actions_enabled()
        self.setWindowTitle(
            f"双轨音频剪辑器 - 原版:{os.path.basename(self.track1.filepath or '未加载')} | "
            f"伴奏:{os.path.basename(self.track2.filepath or '未加载')}"
        )

    # ---------------- 选区 / 撤销重做 ----------------
    def _on_selection_changed(self, sel):
        self._update_status()
        self._update_actions_enabled()

    def _on_seek_requested(self, t):
        if self.playing:
            self.stop_playback()
            self.view.playhead = t
            self.toggle_play()
        self._update_status()

    def _snapshot_splits(self):
        return [list(self.view.split_points[0]), list(self.view.split_points[1])]

    def _push_undo(self):
        self.undo_stack.append(
            (self.track1.samples.copy(), self.track2.samples.copy(), self._snapshot_splits())
        )
        if len(self.undo_stack) > MAX_UNDO:
            self.undo_stack.pop(0)
        self.redo_stack.clear()

    def undo(self):
        if not self.undo_stack:
            return
        self.redo_stack.append(
            (self.track1.samples.copy(), self.track2.samples.copy(), self._snapshot_splits())
        )
        s1, s2, splits = self.undo_stack.pop()
        self.track1.set_samples(s1)
        self.track2.set_samples(s2)
        self.view.split_points = splits
        self.view.clear_selection()
        self.view.refresh_after_edit()
        self._update_status()
        self._update_actions_enabled()

    def redo(self):
        if not self.redo_stack:
            return
        self.undo_stack.append(
            (self.track1.samples.copy(), self.track2.samples.copy(), self._snapshot_splits())
        )
        s1, s2, splits = self.redo_stack.pop()
        self.track1.set_samples(s1)
        self.track2.set_samples(s2)
        self.view.split_points = splits
        self.view.clear_selection()
        self.view.refresh_after_edit()
        self._update_status()
        self._update_actions_enabled()

    def split_at_playhead(self):
        if not (self.track1.loaded and self.track2.loaded):
            return
        if not self.view.can_split(self.view.playhead):
            return
        self.stop_playback()
        self._push_undo()
        self.view.add_split_point(self.view.playhead)
        self._update_actions_enabled()

    def delete_selection(self):
        sel = self.view.selection
        if not sel or not (self.track1.loaded and self.track2.loaded):
            return
        start, end = sel
        if end <= start:
            return
        lane = self.view.selection_lane
        track = self.track1 if lane == 0 else self.track2
        self.stop_playback()
        self._push_undo()
        track.silence_range(start, end)
        self.view.playhead = start
        self.view.clear_selection()
        self.view.refresh_after_edit()
        self._update_status()
        self._update_actions_enabled()

    def trim_selection(self):
        sel = self.view.selection
        if not sel or not (self.track1.loaded and self.track2.loaded):
            return
        start, end = sel
        if end <= start:
            return
        lane = self.view.selection_lane
        track = self.track1 if lane == 0 else self.track2
        self.stop_playback()
        self._push_undo()
        track.crop_to(start, end)
        self.view.apply_crop_to_splits(lane, start, end)
        self.view.playhead = 0.0
        self.view.clear_selection()
        self.view.zoom_fit()
        self._update_status()
        self._update_actions_enabled()

    # ---------------- 静音 ----------------
    def _on_mute1(self, checked):
        self.mute1 = checked

    def _on_mute2(self, checked):
        self.mute2 = checked

    # ---------------- 播放 ----------------
    def _build_mix(self, start_t, end_t):
        s1 = self.track1.time_to_sample(start_t)
        e1 = self.track1.time_to_sample(end_t)
        s2 = self.track2.time_to_sample(start_t)
        e2 = self.track2.time_to_sample(end_t)
        a = self.track1.samples[s1:e1]
        b = self.track2.samples[s2:e2]
        # 两条轨道现在可能各自被单独剪辑，长度不再一致：按较长的一条来混音，
        # 较短的一条播完自己的部分后保持静音，而不是把另一条也截断。
        n = max(len(a), len(b))
        mix = np.zeros((n, self.track1.channels), dtype=np.float32)
        if not self.mute1:
            mix[:len(a)] += a
        if not self.mute2:
            mix[:len(b)] += b
        return np.clip(mix, -1.0, 1.0)

    def toggle_play(self):
        if not (self.track1.loaded and self.track2.loaded):
            return
        if self.playing:
            self._pause_playback()
        else:
            self._start_playback()

    def _start_playback(self):
        sel = self.view.selection
        total = self.view.total_duration()
        if sel and (sel[1] - sel[0]) > 0.02:
            start_t, end_t = sel
        else:
            start_t = self.view.playhead
            end_t = total
        if end_t <= start_t:
            start_t, end_t = 0.0, total
        mix = self._build_mix(start_t, end_t)
        if len(mix) == 0:
            return
        sd.play(mix, samplerate=TARGET_SR)
        self._play_wall_start = time.time()
        self._play_pos_start = start_t
        self._play_end = end_t
        self.playing = True
        self.play_timer.start(30)
        self.act_play.setText("暂停")

    def _pause_playback(self):
        sd.stop()
        elapsed = time.time() - self._play_wall_start
        self.view.playhead = min(self._play_end, self._play_pos_start + elapsed)
        self.playing = False
        self.play_timer.stop()
        self.act_play.setText("播放")
        self.view.update()
        self._update_status()

    def stop_playback(self):
        sd.stop()
        self.playing = False
        self.play_timer.stop()
        self.act_play.setText("播放")
        self._update_status()

    def _on_play_tick(self):
        elapsed = time.time() - self._play_wall_start
        pos = self._play_pos_start + elapsed
        if pos >= self._play_end:
            self.view.playhead = self._play_end
            self.stop_playback()
        else:
            self.view.playhead = pos
        self.view.update()
        self._update_status()

    # ---------------- 导出 ----------------
    def export_mixed(self):
        if not (self.track1.loaded and self.track2.loaded):
            return
        base1 = os.path.splitext(os.path.basename(self.track1.filepath or "track1"))[0]
        path, _ = QFileDialog.getSaveFileName(
            self, "导出合并音轨", f"{base1}_mix.mp3",
            "MP3 (*.mp3);;WAV (*.wav)",
        )
        if not path:
            return
        mix = self._build_mix(0.0, self.view.total_duration())
        try:
            export_samples(mix, path, TARGET_SR, self.track1.channels)
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        QMessageBox.information(self, "导出完成", f"已导出：\n{path}")

    # ---------------- 状态栏 ----------------
    def _update_status(self):
        total = self.view.total_duration()
        pos_text = f"位置 {fmt_time(self.view.playhead)} / {fmt_time(total)}"
        sel = self.view.selection
        if sel and sel[1] > sel[0]:
            sel_text = f"  |  选区 {fmt_time(sel[0])} - {fmt_time(sel[1])} (时长 {fmt_time(sel[1]-sel[0])})"
        else:
            sel_text = ""
        self.status_label.setText(pos_text + sel_text)
        self._sync_scrollbar()

    def _sync_scrollbar(self):
        total = self.view.total_duration()
        dur = self.view.view_end - self.view.view_start
        max_start = max(0.0, total - dur)
        self.scrollbar.blockSignals(True)
        self.scrollbar.setRange(0, int(round(max_start * SCROLL_SCALE)))
        self.scrollbar.setPageStep(max(1, int(round(dur * SCROLL_SCALE))))
        self.scrollbar.setValue(int(round(self.view.view_start * SCROLL_SCALE)))
        self.scrollbar.setEnabled(max_start > 0)
        self.scrollbar.blockSignals(False)

    def _on_scrollbar_changed(self, value):
        self.view.set_view_start(value / SCROLL_SCALE)

    def closeEvent(self, event):
        sd.stop()
        super().closeEvent(event)
