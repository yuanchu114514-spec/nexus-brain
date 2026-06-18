"""迷你桌面宠物窗口 — Layer 1 休眠态，始终可见的浮动窗口。白色圆形底座 + 蓝色四芒星。"""

from __future__ import annotations

import math
import os
import webbrowser

from PyQt5.QtWidgets import (
    QWidget,
    QMenu,
    QApplication,
)
from PyQt5.QtCore import (
    Qt,
    pyqtSignal,
    QPoint,
    QPropertyAnimation,
    QEasingCurve,
    pyqtProperty,
)
from PyQt5.QtGui import (
    QPainter,
    QPainterPath,
    QColor,
)

SIZE = 56
ACCENT_BLUE = "#5B9BD5"
WINDOW_SIZE = SIZE + 12  # 68, provides 6 px margin for shadow


class MiniWindow(QWidget):
    """Layer 1 迷你窗口 — 桌面边缘常驻，点击展开 Live2D 角色。"""

    clicked = pyqtSignal()
    hide_requested = pyqtSignal()
    quit_requested = pyqtSignal()
    settings_requested = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._drag_start: QPoint | None = None
        self._drag_offset: QPoint | None = None
        self._web_chat_url = "http://localhost:8080"
        self._char_name = ""
        self._memory_folder = ""
        self._angle = 0.0  # current rotation angle in degrees

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFixedSize(WINDOW_SIZE, WINDOW_SIZE)

        self._setup_animation()
        self._setup_menu()

    # ── pyqtProperty for rotation animation ──

    @pyqtProperty(float)
    def _rotation_angle(self) -> float:
        return self._angle

    @_rotation_angle.setter
    def _rotation_angle(self, value: float):
        self._angle = value
        self.update()

    # ── setup ──

    def _setup_animation(self):
        self._hover_anim = QPropertyAnimation(self, b"_rotation_angle")
        self._hover_anim.setDuration(1000)
        self._hover_anim.setEasingCurve(QEasingCurve.InOutSine)

    def _setup_menu(self):
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)

    # ── paint ──

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        margin = (WINDOW_SIZE - SIZE) // 2  # = 6
        cx = WINDOW_SIZE / 2.0
        cy = WINDOW_SIZE / 2.0

        # ── manual soft shadow (QGraphicsDropShadowEffect incompatible with WA_TranslucentBackground) ──
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 25))
        p.drawEllipse(margin + 1, margin + 2, SIZE, SIZE)

        # ── white circular base ──
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(margin, margin, SIZE, SIZE)

        # ── blue four-pointed star (rotated) ──
        r_outer = SIZE / 2.0 - 4  # = 24
        r_inner = r_outer * 0.38   # ≈ 9.12

        p.save()
        p.translate(cx, cy)
        p.rotate(self._angle)
        p.translate(-cx, -cy)

        path = QPainterPath()
        for i in range(8):
            angle = -math.pi / 2.0 + i * math.pi / 4.0
            r = r_outer if i % 2 == 0 else r_inner
            x = cx + r * math.cos(angle)
            y = cy + r * math.sin(angle)
            if i == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        path.closeSubpath()

        p.setPen(Qt.NoPen)
        p.setBrush(QColor(ACCENT_BLUE))
        p.drawPath(path)

        p.restore()
        p.end()

    # ── hover animation ──

    def enterEvent(self, event):
        self._hover_anim.stop()
        self._hover_anim.setStartValue(self._angle)
        self._hover_anim.setEndValue(4.0)
        self._hover_anim.start()

    def leaveEvent(self, event):
        self._hover_anim.stop()
        self._hover_anim.setStartValue(self._angle)
        self._hover_anim.setEndValue(0.0)
        self._hover_anim.start()

    # ── drag & click ──

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_start = event.globalPos()
            self._drag_offset = event.globalPos() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton and self._drag_offset is not None:
            self.move(event.globalPos() - self._drag_offset)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._drag_start is not None:
            delta = event.globalPos() - self._drag_start
            if delta.manhattanLength() < 5:
                self.clicked.emit()
            else:
                self._snap_to_edge()
        self._drag_start = None
        self._drag_offset = None

    def _snap_to_edge(self):
        geo = self.frameGeometry()
        screen = QApplication.screenAt(geo.center())
        if screen is None:
            screen = QApplication.primaryScreen()
        sg = screen.availableGeometry()

        SNAP_MARGIN = 40
        EDGE_MARGIN = 8
        x, y = geo.x(), geo.y()
        w, h = geo.width(), geo.height()

        if abs(x - sg.left()) < SNAP_MARGIN:
            x = sg.left() + EDGE_MARGIN
        elif abs(x + w - sg.right()) < SNAP_MARGIN:
            x = sg.right() - w - EDGE_MARGIN

        if abs(y - sg.top()) < SNAP_MARGIN:
            y = sg.top() + EDGE_MARGIN
        elif abs(y + h - sg.bottom()) < SNAP_MARGIN:
            y = sg.bottom() - h - EDGE_MARGIN

        self.move(x, y)

    # ── context menu ──

    def _show_menu(self, pos):
        menu = QMenu()
        menu.setStyleSheet("""
            QMenu {
                background: #1e1e2e;
                color: #e8e0f0;
                border: 1px solid rgba(255,255,255,20);
                border-radius: 8px;
                padding: 4px;
            }
            QMenu::item {
                padding: 8px 24px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background: #5B9BD5;
            }
        """)

        web_chat = menu.addAction("💬 打开 Web Chat")
        web_chat.triggered.connect(lambda: webbrowser.open(self._web_chat_url))

        menu.addSeparator()

        settings = menu.addAction("个性化设置")
        settings.triggered.connect(self.settings_requested.emit)

        if self._memory_folder:
            folder = self._memory_folder
            memory = menu.addAction("📖 打开记忆文件夹")
            memory.triggered.connect(lambda f=folder: os.startfile(f))

        menu.addSeparator()

        hide = menu.addAction("👁 暂时隐藏")
        hide.triggered.connect(self.hide_requested.emit)

        quit_act = menu.addAction("❌ 退出")
        quit_act.triggered.connect(self.quit_requested.emit)

        menu.exec_(self.mapToGlobal(pos))

    # ── setters ──

    def set_char_name(self, name: str):
        self._char_name = name

    def set_memory_folder(self, path: str):
        self._memory_folder = path

    def set_web_chat_url(self, url: str):
        self._web_chat_url = url

    # ── position persistence ──

    def save_position(self) -> dict:
        geo = self.frameGeometry()
        return {"x": geo.x(), "y": geo.y()}

    def restore_position(self, x: int | None, y: int | None):
        if x is not None and y is not None:
            self.move(x, y)
        else:
            screen = QApplication.primaryScreen().geometry()
            self.move(screen.width() - WINDOW_SIZE - 30, screen.height() // 3)
