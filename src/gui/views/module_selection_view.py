import json
import logging
from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QTreeWidget, QTreeWidgetItem, QPushButton, QLabel

class ModuleSelectionView(QWidget):
    """Third screen: allows selection of modules and lessons."""
    download_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._courses_by_id = {}
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Selecione o conteúdo a ser baixado:"))

        self.tree_widget = QTreeWidget()
        self.tree_widget.setHeaderLabel("Conteúdo do Curso")
        self.tree_widget.itemChanged.connect(self._on_item_changed)

        btn_layout = QHBoxLayout()
        self.btn_select_all = QPushButton("Selecionar Tudo")
        self.btn_select_all.clicked.connect(self._select_all)
        self.btn_deselect_all = QPushButton("Deselecionar Tudo")
        self.btn_deselect_all.clicked.connect(self._deselect_all)
        
        self.btn_expand_all = QPushButton("Expandir Tudo")
        self.btn_expand_all.clicked.connect(self.tree_widget.expandAll)
        self.btn_collapse_all = QPushButton("Colapsar Tudo")
        self.btn_collapse_all.clicked.connect(self.tree_widget.collapseAll)

        btn_layout.addWidget(self.btn_select_all)
        btn_layout.addWidget(self.btn_deselect_all)
        btn_layout.addWidget(self.btn_expand_all)
        btn_layout.addWidget(self.btn_collapse_all)
        layout.addLayout(btn_layout)

        layout.addWidget(self.tree_widget)

        self.download_button = QPushButton("Baixar Selecionados")
        self.download_button.clicked.connect(self._on_download)

        layout.addWidget(self.tree_widget)
        layout.addWidget(self.download_button)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        """Handles changes in item's check state to update parents and children."""
        # Recursão
        self.tree_widget.blockSignals(True)

        new_state = item.checkState(column)
        if new_state != Qt.CheckState.PartiallyChecked:
            for i in range(item.childCount()):
                child = item.child(i)
                child.setCheckState(column, new_state)

        parent = item.parent()
        if parent:
            self._update_parent_state(parent, column)

        self.tree_widget.blockSignals(False)

    def _update_parent_state(self, parent: QTreeWidgetItem, column: int) -> None:
        """Recursively updates the parent's check state based on its children's states."""
        child_states = [parent.child(i).checkState(column) for i in range(parent.childCount())]

        all_checked = all(state == Qt.CheckState.Checked for state in child_states)
        all_unchecked = all(state == Qt.CheckState.Unchecked for state in child_states)

        self.tree_widget.blockSignals(True)
        if all_checked:
            parent.setCheckState(column, Qt.CheckState.Checked)
        elif all_unchecked:
            parent.setCheckState(column, Qt.CheckState.Unchecked)
        else:
            parent.setCheckState(column, Qt.CheckState.PartiallyChecked)
        self.tree_widget.blockSignals(False)

        grandparent = parent.parent()
        if grandparent:
            self._update_parent_state(grandparent, column)

    def update_modules(self, content: dict, courses: list) -> None:
        """Clears the tree and populates it with course modules and lessons."""
        self.tree_widget.clear()
        self._courses_by_id = {str(course["id"]): course for course in courses}

        for course_id, course_data in content.items():
            course_item = QTreeWidgetItem(self.tree_widget, [course_data["title"]])
            course_item.setData(0, Qt.ItemDataRole.UserRole, {"id": course_id})
            course_item.setFlags(course_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            course_item.setCheckState(0, Qt.CheckState.Checked)

            for module in course_data.get("modules", []):
                module_item = QTreeWidgetItem(course_item, [module["title"]])
                module_item.setData(0, Qt.ItemDataRole.UserRole, module)
                module_item.setFlags(module_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                module_item.setCheckState(0, Qt.CheckState.Checked)
                
                for lesson in module.get("lessons", []):
                    lesson_item = QTreeWidgetItem(module_item, [lesson["title"]])
                    lesson_item.setData(0, Qt.ItemDataRole.UserRole, lesson)
                    lesson_item.setFlags(lesson_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    lesson_item.setCheckState(0, Qt.CheckState.Checked)
        
        self.tree_widget.expandAll()

    def _select_all(self) -> None:
        self._set_all_check_state(Qt.CheckState.Checked)

    def _deselect_all(self) -> None:
        self._set_all_check_state(Qt.CheckState.Unchecked)

    def _set_all_check_state(self, state: Qt.CheckState) -> None:
        self.tree_widget.blockSignals(True)
        root = self.tree_widget.invisibleRootItem()
        self._recursive_set_state(root, state)
        self.tree_widget.blockSignals(False)

    def _recursive_set_state(self, item: QTreeWidgetItem, state: Qt.CheckState) -> None:
        for i in range(item.childCount()):
            child = item.child(i)
            child.setCheckState(0, state)
            self._recursive_set_state(child, state)

    def _on_download(self) -> None:
        """
        Collects all items, adding a 'download' flag based on checkbox state,
        and emits a signal with the complete data structure.
        """
        selection = {}
        root = self.tree_widget.invisibleRootItem()

        for i in range(root.childCount()):
            course_item = root.child(i)
            course_data = course_item.data(0, Qt.ItemDataRole.UserRole)
            course_id = course_data["id"]
            full_course_data = self._courses_by_id.get(str(course_id), {}).copy()
            full_course_data["modules"] = []

            selection[course_id] = full_course_data

            for j in range(course_item.childCount()):
                module_item = course_item.child(j)
                module_data = module_item.data(0, Qt.ItemDataRole.UserRole).copy()

                module_data["download"] = module_item.checkState(0) in (Qt.CheckState.Checked, Qt.CheckState.PartiallyChecked)
                module_locked = module_data.get("locked", False)
                if module_locked:
                    module_data["download"] = False

                if "lessons" not in module_data:
                    module_data["lessons"] = []

                modified_lessons = []
                for k in range(module_item.childCount()):
                    lesson_item = module_item.child(k)
                    lesson_data = lesson_item.data(0, Qt.ItemDataRole.UserRole).copy()

                    is_checked = lesson_item.checkState(0) == Qt.CheckState.Checked
                    is_locked = lesson_data.get("locked", False)
                    lesson_data["download"] = is_checked and not is_locked
                    
                    modified_lessons.append(lesson_data)
                
                module_data["lessons"] = modified_lessons
                full_course_data["modules"].append(module_data)

        selection_json = json.dumps(selection, indent=2)
        logging.debug("\n--- DEBUG: Content Selected for Download ---")
        logging.debug(selection_json)
        logging.debug("------------------------------------------\n")

        self.download_requested.emit(selection_json)
