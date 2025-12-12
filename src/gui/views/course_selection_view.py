from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QListWidget, QPushButton, QLabel, QListWidgetItem
from typing import List, Dict, Any

class CourseSelectionView(QWidget):
    """Second screen: allows user to select courses."""
    courses_selected = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Selecione os cursos para download:"))

        self.course_list = QListWidget()
        self.course_list.setSelectionMode(QListWidget.SelectionMode.MultiSelection)

        self.next_button = QPushButton("Selecionar Módulos/Aulas")
        self.next_button.clicked.connect(self._on_next)
        
        layout.addWidget(self.course_list)
        layout.addWidget(self.next_button)

    def update_courses(self, courses: List[Dict[str, Any]]) -> None:
        """Clears and repopulates the course list widget."""
        self.course_list.clear()
        for course in courses:
            item_name = course.get("name", "Unnamed Course") + " - " + course.get("seller_name", "Unknown Seller")
            item = QListWidgetItem(item_name)
            item.setData(Qt.ItemDataRole.UserRole, course)
            self.course_list.addItem(item)

    def _on_next(self) -> None:
        """Emits the data of the selected courses."""
        selected_courses = []
        for item in self.course_list.selectedItems():
            course_data = item.data(Qt.ItemDataRole.UserRole)
            if course_data:
                selected_courses.append(course_data)
        self.courses_selected.emit(selected_courses)
