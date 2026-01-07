from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QListWidget, QPushButton, QLabel, QListWidgetItem, QHBoxLayout, QLineEdit, QCheckBox
from typing import List, Dict, Any

class CourseSelectionView(QWidget):
    """Second screen: allows user to select courses."""
    courses_selected = Signal(list)
    search_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)

        search_layout = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Pesquisar curso...")
        self.search_input.returnPressed.connect(self._perform_search)
        
        self.search_button = QPushButton("Pesquisar")
        self.search_button.clicked.connect(self._perform_search)
        
        self.platform_search_checkbox = QCheckBox("Listar conteudo novamente da plataforma para localizar o item (evite)")
        self.platform_search_checkbox.setToolTip("Se marcado, a pesquisa será feita diretamente na plataforma.")

        search_layout.addWidget(QLabel("Pesquisar:"))
        search_layout.addWidget(self.search_input)
        search_layout.addWidget(self.search_button)
        search_layout.addWidget(self.platform_search_checkbox)
        layout.addLayout(search_layout)
        
        layout.addWidget(QLabel("Selecione os cursos para download:"))

        self.course_list = QListWidget()
        self.course_list.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        
        self._all_courses: List[Dict[str, Any]] = []

        self.next_button = QPushButton("Selecionar Módulos/Aulas")
        self.next_button.clicked.connect(self._on_next)
        
        layout.addWidget(self.course_list)
        layout.addWidget(self.next_button)

    def update_courses(self, courses: List[Dict[str, Any]]) -> None:
        """Clears and repopulates the course list widget."""
        self._all_courses = courses
        self._filter_items(self.search_input.text())

    def _perform_search(self) -> None:
        """Executes the search or filter logic."""
        text = self.search_input.text().strip()
        if self.platform_search_checkbox.isChecked():
            self.search_requested.emit(text)
        else:
            self._filter_items(text)

    def _filter_items(self, text: str) -> None:
        """Filters the course list based on the search text locally."""
        self.course_list.clear()
        search_text = text.lower()
        
        for course in self._all_courses:
            item_name = course.get("name", "Unnamed Course") + " - " + course.get("seller_name", "Unknown Seller")
            if search_text in item_name.lower():
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
