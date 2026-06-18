"""迷你个性化设置对话框 — 角色名 + 记忆文件夹。"""

from __future__ import annotations

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QFileDialog,
)
from PyQt5.QtCore import Qt


class SettingsDialog(QDialog):
    """个性化设置对话框，配置角色名和记忆文件夹路径。"""

    def __init__(self, parent=None, char_name: str = "Nexus",
                 memory_folder: str = ""):
        super().__init__(parent)
        self.setWindowTitle("个性化设置")
        self.setFixedSize(380, 200)
        self.setWindowFlags(
            self.windowFlags() & ~Qt.WindowContextHelpButtonHint
        )
        self.setStyleSheet("""
            QDialog {
                background: #1e1e2e;
                color: #e8e0f0;
            }
            QLabel {
                color: #cdd6f4;
                font-size: 13px;
            }
            QLineEdit {
                background: #313244;
                color: #e8e0f0;
                border: 1px solid #45475a;
                border-radius: 6px;
                padding: 6px 10px;
                font-size: 13px;
            }
            QLineEdit:focus {
                border-color: #5B9BD5;
            }
            QPushButton {
                background: #45475a;
                color: #e8e0f0;
                border: none;
                border-radius: 6px;
                padding: 6px 14px;
                font-size: 13px;
            }
            QPushButton:hover {
                background: #585b70;
            }
            QPushButton#saveBtn {
                background: #5B9BD5;
                color: #fff;
            }
            QPushButton#saveBtn:hover {
                background: #7BB3E0;
            }
        """)

        self._char_name = char_name
        self._memory_folder = memory_folder

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 16, 20, 16)

        # ── 角色名 ──
        name_row = QHBoxLayout()
        name_label = QLabel("角色名:")
        name_label.setFixedWidth(72)
        name_row.addWidget(name_label)

        self.name_input = QLineEdit(char_name)
        self.name_input.setPlaceholderText("输入你的角色名...")
        name_row.addWidget(self.name_input)
        layout.addLayout(name_row)

        # ── 记忆文件夹 ──
        folder_row = QHBoxLayout()
        folder_label = QLabel("记忆文件夹:")
        folder_label.setFixedWidth(72)
        folder_row.addWidget(folder_label)

        self.folder_input = QLineEdit(memory_folder)
        self.folder_input.setPlaceholderText("选择记忆文件夹路径...")
        self.folder_input.setReadOnly(True)
        folder_row.addWidget(self.folder_input)

        browse_btn = QPushButton("📂")
        browse_btn.setFixedWidth(40)
        browse_btn.clicked.connect(self._browse_folder)
        folder_row.addWidget(browse_btn)
        layout.addLayout(folder_row)

        # ── 联动提示 ──
        self.notebook_hint = QLabel(self._build_hint())
        self.notebook_hint.setStyleSheet("color: #a6adc8; font-size: 12px;")
        layout.addWidget(self.notebook_hint)

        # 角色名变更时实时更新提示
        self.name_input.textChanged.connect(self._on_name_changed)

        layout.addStretch()

        # ── 按钮 ──
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        reset_btn = QPushButton("重置")
        reset_btn.clicked.connect(self._reset)
        btn_row.addWidget(reset_btn)

        self.save_btn = QPushButton("保存")
        self.save_btn.setObjectName("saveBtn")
        self.save_btn.clicked.connect(self._save)
        btn_row.addWidget(self.save_btn)

        layout.addLayout(btn_row)

    def _build_hint(self):
        name = self.name_input.text().strip() or "?"
        folder = self.folder_input.text().strip()
        if folder:
            return f"→ 小本本: {folder}/{name}的小本本.md"
        return f"→ 小本本: {name}的小本本.md（请先选择记忆文件夹）"

    def _on_name_changed(self, _text):
        self.notebook_hint.setText(self._build_hint())

    def _browse_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "选择记忆文件夹",
            self.folder_input.text() or "",
        )
        if folder:
            self.folder_input.setText(folder)
            self.notebook_hint.setText(self._build_hint())

    def _save(self):
        self._char_name = self.name_input.text().strip() or "Nexus"
        self._memory_folder = self.folder_input.text().strip()
        self.accept()

    def _reset(self):
        self.name_input.setText("Nexus")
        self.folder_input.setText("")
        self.notebook_hint.setText(self._build_hint())

    def get_values(self) -> tuple[str, str]:
        """返回 (char_name, memory_folder)。"""
        return self._char_name, self._memory_folder
