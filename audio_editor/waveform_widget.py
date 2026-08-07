"""双轨波形显示与选区交互控件。"""
from PyQt5.QtCore import Qt, QRectF, pyqtSignal
from PyQt5.QtGui import QPainter, QColor, QPen
from PyQt5.QtWidgets import QWidget

NICE_STEPS = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600]


def nice_tick_interval(view_duration, width_px):
    if view_duration <= 0 or width_px <= 0:
        return 1.0
    target_px_per_tick = 90
    approx = view_duration / max(1, width_px / target_px_per_tick)
    for step in NICE_STEPS:
        if step >= approx:
            return step
    return NICE_STEPS[-1]


def fmt_time(t):
    t = max(0.0, t)
    m = int(t // 60)
    s = t - m * 60
    return f"{m:02d}:{s:05.2f}"


class DualWaveformView(QWidget):
    selectionChanged = pyqtSignal(object)  # (start, end) 或 None
    seekRequested = pyqtSignal(float)
    viewChanged = pyqtSignal()

    RULER_H = 26

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumHeight(320)
        self.setFocusPolicy(Qt.StrongFocus)

        self.track1 = None
        self.track2 = None
        self.view_start = 0.0
        self.view_end = 1.0
        self.selection = None  # (start, end) 秒，只作用于 selection_lane 那一条轨道
        self.selection_lane = 0  # 0 = 上方（原版），1 = 下方（伴奏）
        self.playhead = 0.0
        self.split_points = [[], []]  # 每条轨道各自的拆分点（秒）

        self._drag_anchor = None
        self._drag_lane = None
        self._env_cache = {}

    # ---------- 数据 ----------
    def set_tracks(self, track1, track2):
        self.track1 = track1
        self.track2 = track2
        self.selection = None
        self.selection_lane = 0
        self.playhead = 0.0
        self.split_points = [[], []]
        self._env_cache = {}
        self.zoom_fit()

    def refresh_after_edit(self):
        self._env_cache = {}
        total = self.total_duration()
        if self.view_end > total:
            self.view_end = total
        if self.view_start > self.view_end:
            self.view_start = max(0.0, self.view_end - 0.001)
        if self.playhead > total:
            self.playhead = total
        d1 = self.track1.duration if self.track1 else 0.0
        d2 = self.track2.duration if self.track2 else 0.0
        self.split_points[0] = [p for p in self.split_points[0] if 0 < p < d1]
        self.split_points[1] = [p for p in self.split_points[1] if 0 < p < d2]
        self.update()

    def total_duration(self):
        d1 = self.track1.duration if self.track1 else 0.0
        d2 = self.track2.duration if self.track2 else 0.0
        return max(d1, d2)

    def track_duration(self, lane):
        track = self.track1 if lane == 0 else self.track2
        return track.duration if track else 0.0

    # ---------- 拆分点 / 块（每条轨道独立） ----------
    def _can_split_lane(self, lane, t):
        dur = self.track_duration(lane)
        if t <= 1e-3 or t >= dur - 1e-3:
            return False
        return not any(abs(t - p) < 0.02 for p in self.split_points[lane])

    def can_split(self, t):
        return self._can_split_lane(0, t) or self._can_split_lane(1, t)

    def add_split_point(self, t):
        """在两条轨道里，凡是该时间点有效的都加入拆分点。"""
        added = False
        for lane in (0, 1):
            if self._can_split_lane(lane, t):
                self.split_points[lane].append(t)
                self.split_points[lane].sort()
                added = True
        if added:
            self.update()
        return added

    def block_at(self, t, lane):
        """返回时间点 t 在指定轨道所在的整块区间 (start, end)。"""
        total = self.track_duration(lane)
        bounds = [0.0] + sorted(self.split_points[lane]) + [total]
        for i in range(len(bounds) - 1):
            if bounds[i] - 1e-6 <= t <= bounds[i + 1] + 1e-6:
                return bounds[i], bounds[i + 1]
        return 0.0, total

    def apply_crop_to_splits(self, lane, start, end):
        """裁剪保留 [start, end) 后，同步平移该轨道的拆分点，范围外的丢弃。"""
        new_points = []
        for p in self.split_points[lane]:
            if start < p < end:
                new_points.append(round(p - start, 6))
        self.split_points[lane] = sorted(set(new_points))

    # ---------- 视图/缩放 ----------
    def zoom_fit(self):
        self.view_start = 0.0
        self.view_end = max(self.total_duration(), 0.001)
        self._env_cache = {}
        self.update()
        self.viewChanged.emit()

    def zoom(self, factor, center_time=None):
        total = self.total_duration()
        if total <= 0:
            return
        dur = self.view_end - self.view_start
        if center_time is None:
            center_time = (self.view_start + self.view_end) / 2
        new_dur = dur * factor
        new_dur = max(0.05, min(total, new_dur))
        ratio = (center_time - self.view_start) / dur if dur > 0 else 0.5
        new_start = center_time - ratio * new_dur
        new_end = new_start + new_dur
        if new_start < 0:
            new_end -= new_start
            new_start = 0
        if new_end > total:
            shift = new_end - total
            new_start = max(0, new_start - shift)
            new_end = total
        self.view_start, self.view_end = new_start, new_end
        self._env_cache = {}
        self.update()
        self.viewChanged.emit()

    def pan(self, delta_seconds):
        self.set_view_start(self.view_start + delta_seconds)

    def set_view_start(self, new_start):
        total = self.total_duration()
        dur = self.view_end - self.view_start
        new_start = max(0, min(total - dur, new_start)) if total > dur else 0
        self.view_start = new_start
        self.view_end = new_start + dur
        self._env_cache = {}
        self.update()
        self.viewChanged.emit()

    # ---------- 坐标换算 ----------
    def x_to_time(self, x):
        w = max(1, self.width())
        dur = self.view_end - self.view_start
        return self.view_start + (x / w) * dur

    def time_to_x(self, t):
        w = self.width()
        dur = self.view_end - self.view_start
        if dur <= 0:
            return 0
        return (t - self.view_start) / dur * w

    def set_selection(self, sel, lane=None):
        self.selection = sel
        if lane is not None:
            self.selection_lane = lane
        self.update()
        self.selectionChanged.emit(sel)

    def clear_selection(self):
        self.set_selection(None)

    # ---------- 绘制 ----------
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(24, 24, 28))

        w = self.width()
        h = self.height()
        lane_h = (h - self.RULER_H) // 2
        lane_ys = [self.RULER_H, self.RULER_H + lane_h]
        lane_hs = [lane_h, h - self.RULER_H - lane_h]  # 下方轨道吃掉多余像素，不留空隙

        self._draw_ruler(painter, w)

        self._draw_lane(painter, self.track1, "原版 / 原唱", 0, lane_ys[0], w, lane_hs[0],
                         QColor(80, 160, 255))
        self._draw_lane(painter, self.track2, "伴奏", 1, lane_ys[1], w, lane_hs[1],
                         QColor(120, 220, 140))

        # 拆分点（每条轨道各画各的）
        pen = QPen(QColor(255, 255, 255, 200))
        pen.setWidth(1)
        pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        for lane in (0, 1):
            y0, y1 = lane_ys[lane], lane_ys[lane] + lane_hs[lane]
            for p in self.split_points[lane]:
                if self.view_start <= p <= self.view_end:
                    x = self.time_to_x(p)
                    painter.drawLine(int(x), y0, int(x), y1)

        # 选区：只覆盖所属的那一条轨道
        if self.selection:
            s, e = self.selection
            x1 = self.time_to_x(max(s, self.view_start))
            x2 = self.time_to_x(min(e, self.view_end))
            if x2 > x1:
                y0, y1 = lane_ys[self.selection_lane], lane_ys[self.selection_lane] + lane_hs[self.selection_lane]
                painter.fillRect(QRectF(x1, y0, x2 - x1, y1 - y0),
                                  QColor(255, 220, 80, 70))
                pen = QPen(QColor(255, 220, 80, 180))
                pen.setWidth(1)
                painter.setPen(pen)
                painter.drawLine(int(x1), y0, int(x1), y1)
                painter.drawLine(int(x2), y0, int(x2), y1)

        # 播放头
        if self.view_start <= self.playhead <= self.view_end:
            x = self.time_to_x(self.playhead)
            pen = QPen(QColor(255, 70, 70))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawLine(int(x), self.RULER_H, int(x), h)

        painter.end()

    def _draw_ruler(self, painter, w):
        painter.fillRect(QRectF(0, 0, w, self.RULER_H), QColor(15, 15, 18))
        dur = self.view_end - self.view_start
        if dur <= 0:
            return
        step = nice_tick_interval(dur, w)
        painter.setPen(QPen(QColor(160, 160, 170)))
        t = (int(self.view_start / step)) * step
        while t <= self.view_end + step:
            if t >= self.view_start:
                x = self.time_to_x(t)
                painter.drawLine(int(x), self.RULER_H - 6, int(x), self.RULER_H)
                painter.drawText(int(x) + 3, self.RULER_H - 8, fmt_time(t))
            t += step

    def _draw_lane(self, painter, track, label, cache_key, y, w, lane_h, color):
        painter.setPen(QPen(QColor(50, 50, 56)))
        painter.drawRect(0, y, w - 1, lane_h - 1)

        mid = y + lane_h / 2
        painter.setPen(QPen(QColor(60, 60, 66)))
        painter.drawLine(0, int(mid), w, int(mid))

        painter.setPen(QPen(QColor(200, 200, 210)))
        painter.drawText(6, y + 14, label if track and track.loaded else f"{label}（未加载）")

        if not track or not track.loaded or w <= 0:
            return

        cache_id = (cache_key, self.view_start, self.view_end, w)
        cached = self._env_cache.get(cache_key)
        if cached and cached[0] == cache_id:
            mins, maxs = cached[1], cached[2]
        else:
            mins, maxs = track.envelope(self.view_start, self.view_end, w)
            self._env_cache[cache_key] = (cache_id, mins, maxs)

        amp = (lane_h / 2 - 4)
        painter.setPen(QPen(color))
        for x in range(min(w, len(mins))):
            y1 = mid - maxs[x] * amp
            y2 = mid - mins[x] * amp
            if abs(y2 - y1) < 1:
                y2 = y1 + 1
            painter.drawLine(x, int(y1), x, int(y2))

    # ---------- 鼠标交互 ----------
    def _lane_at_y(self, y):
        h = self.height()
        lane_h = (h - self.RULER_H) // 2
        return 0 if y < self.RULER_H + lane_h else 1

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            t = self.x_to_time(event.x())
            lane = self._lane_at_y(event.y())
            self._drag_anchor = t
            self._drag_lane = lane
            self.set_selection((t, t), lane)
        self.setFocus()

    def mouseMoveEvent(self, event):
        if self._drag_anchor is not None:
            t = self.x_to_time(event.x())
            a = self._drag_anchor
            self.set_selection((min(a, t), max(a, t)), self._drag_lane)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._drag_anchor is not None:
            t = self.x_to_time(event.x())
            a = self._drag_anchor
            lane = self._drag_lane
            ctrl_held = bool(event.modifiers() & Qt.ControlModifier)
            self._drag_anchor = None
            self._drag_lane = None
            start, end = min(a, t), max(a, t)
            min_pixels_as_click = 3
            if self.time_to_x(end) - self.time_to_x(start) <= min_pixels_as_click:
                if ctrl_held and self.split_points[lane]:
                    # Ctrl+单击：选中光标所在轨道的那一整块，方便直接删除
                    self.set_selection(self.block_at(a, lane), lane)
                else:
                    # 普通单击：始终放置播放线
                    self.clear_selection()
                    self.playhead = max(0, min(self.total_duration(), a))
                    self.update()
                    self.seekRequested.emit(self.playhead)
            else:
                self.set_selection((start, end), lane)

    def mouseDoubleClickEvent(self, event):
        self.clear_selection()
        t = self.x_to_time(event.x())
        self.playhead = max(0, min(self.total_duration(), t))
        self.update()
        self.seekRequested.emit(self.playhead)

    def wheelEvent(self, event):
        angle = event.angleDelta()
        pixel = event.pixelDelta()

        if event.modifiers() & Qt.ControlModifier:
            delta = angle.y() or angle.x()
            if delta == 0:
                return
            factor = 0.85 if delta > 0 else 1 / 0.85
            self.zoom(factor, self.x_to_time(event.x()))
        else:
            dur = self.view_end - self.view_start
            if not pixel.isNull():
                # 触摸板：像素级平移，横向滑动优先，否则用纵向滑动横移时间轴
                d = pixel.x() if pixel.x() else pixel.y()
                delta_seconds = -d / max(1, self.width()) * dur
            else:
                # 鼠标滚轮 / 部分触摸板：按刻度平移
                d = angle.x() or angle.y()
                delta_seconds = -d / 120.0 * dur * 0.2
            self.pan(delta_seconds)
        event.accept()
