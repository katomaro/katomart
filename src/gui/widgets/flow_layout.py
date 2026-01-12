"""Flow layout and platform tags widget for displaying items that wrap across multiple lines."""

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QLayoutItem, QWidget, QLabel, QSizePolicy


class FlowLayout(QLayout):
    """A layout that arranges widgets in rows, wrapping to new rows as needed.

    Based on Qt's official FlowLayout example.
    """

    def __init__(self, parent: QWidget | None = None, margin: int = -1, h_spacing: int = -1, v_spacing: int = -1):
        """Initialize the FlowLayout.

        Args:
            parent: Parent widget
            margin: Margin around the layout
            h_spacing: Horizontal spacing between items
            v_spacing: Vertical spacing between items
        """
        super().__init__(parent)

        if margin != -1:
            self.setContentsMargins(margin, margin, margin, margin)

        self._h_space = h_spacing
        self._v_space = v_spacing
        self._item_list: list[QLayoutItem] = []

    def __del__(self):
        """Clean up layout items."""
        item = self.takeAt(0)
        while item:
            item = self.takeAt(0)

    def addItem(self, item: QLayoutItem) -> None:
        """Add an item to the layout."""
        self._item_list.append(item)

    def horizontalSpacing(self) -> int:
        """Get horizontal spacing between items."""
        if self._h_space >= 0:
            return self._h_space
        return self._smart_spacing(QLayout.SizeConstraint.SetDefaultConstraint)

    def verticalSpacing(self) -> int:
        """Get vertical spacing between items."""
        if self._v_space >= 0:
            return self._v_space
        return self._smart_spacing(QLayout.SizeConstraint.SetDefaultConstraint)

    def count(self) -> int:
        """Get number of items in the layout."""
        return len(self._item_list)

    def itemAt(self, index: int) -> QLayoutItem | None:
        """Get item at the specified index."""
        if 0 <= index < len(self._item_list):
            return self._item_list[index]
        return None

    def takeAt(self, index: int) -> QLayoutItem | None:
        """Remove and return item at the specified index."""
        if 0 <= index < len(self._item_list):
            return self._item_list.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientation:
        """Return which directions this layout can expand."""
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        """Return whether this layout's height depends on its width."""
        return True

    def heightForWidth(self, width: int) -> int:
        """Calculate the height needed for the given width."""
        height = self._do_layout(QRect(0, 0, width, 0), test_only=True)
        return height

    def setGeometry(self, rect: QRect) -> None:
        """Set the geometry for this layout."""
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:
        """Return the preferred size for this layout."""
        # Calculate size needed if we have a reasonable width (e.g., 400px)
        # This gives a better hint than just the minimum size of a single item
        if self._item_list:
            reasonable_width = 400
            height = self._do_layout(QRect(0, 0, reasonable_width, 0), test_only=True)
            return QSize(reasonable_width, height)
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        """Return the minimum size for this layout."""
        size = QSize()

        for item in self._item_list:
            size = size.expandedTo(item.minimumSize())

        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        """Arrange items in the layout.

        Args:
            rect: Rectangle to lay out items in
            test_only: If True, only calculate height without positioning items

        Returns:
            The height needed for the layout
        """
        left, top, right, bottom = self.getContentsMargins()
        effective_rect = rect.adjusted(left, top, -right, -bottom)
        x = effective_rect.x()
        y = effective_rect.y()
        line_height = 0

        for item in self._item_list:
            widget = item.widget()
            if widget is None:
                continue

            space_x = self.horizontalSpacing()
            if space_x == -1:
                space_x = widget.style().layoutSpacing(
                    QSizePolicy.ControlType.PushButton,
                    QSizePolicy.ControlType.PushButton,
                    Qt.Orientation.Horizontal
                )

            space_y = self.verticalSpacing()
            if space_y == -1:
                space_y = widget.style().layoutSpacing(
                    QSizePolicy.ControlType.PushButton,
                    QSizePolicy.ControlType.PushButton,
                    Qt.Orientation.Vertical
                )

            next_x = x + item.sizeHint().width() + space_x
            if next_x - space_x > effective_rect.right() and line_height > 0:
                x = effective_rect.x()
                y = y + line_height + space_y
                next_x = x + item.sizeHint().width() + space_x
                line_height = 0

            if not test_only:
                item.setGeometry(QRect(QRect(x, y, item.sizeHint().width(), item.sizeHint().height())))

            x = next_x
            line_height = max(line_height, item.sizeHint().height())

        return y + line_height - rect.y() + bottom

    def _smart_spacing(self, pm: QLayout.SizeConstraint) -> int:
        """Get smart spacing from the parent widget's style."""
        parent = self.parent()
        if parent is None:
            return -1
        elif parent.isWidgetType():
            return parent.style().pixelMetric(pm, None, parent)
        else:
            return parent.spacing()


class PlatformTagsWidget(QWidget):
    """A widget that displays platform names as styled tags/chips in a flow layout."""

    def __init__(self, parent: QWidget | None = None):
        """Initialize the PlatformTagsWidget.

        Args:
            parent: Parent widget
        """
        super().__init__(parent)

        self._flow_layout = FlowLayout(self, margin=0, h_spacing=6, v_spacing=6)
        self.setLayout(self._flow_layout)

        # Set size policy to allow vertical expansion
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

    def hasHeightForWidth(self) -> bool:
        """Return whether this widget's height depends on its width."""
        return self._flow_layout.hasHeightForWidth()

    def heightForWidth(self, width: int) -> int:
        """Calculate the height needed for the given width."""
        return self._flow_layout.heightForWidth(width)

    def set_platforms(self, platforms: list[str]) -> None:
        """Set the list of platform names to display.

        Args:
            platforms: List of platform names
        """
        # Clear existing tags
        self.clear_platforms()

        # Add new tags
        if not platforms:
            # Show "Nenhuma" if no platforms
            self._add_tag("Nenhuma", is_empty=True)
        else:
            for platform in sorted(platforms):
                self._add_tag(platform)

        # Force layout recalculation and notify parent of size change
        self._flow_layout.activate()
        self.updateGeometry()

    def clear_platforms(self) -> None:
        """Remove all platform tags from the widget."""
        while self._flow_layout.count():
            item = self._flow_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

    def _add_tag(self, text: str, is_empty: bool = False) -> None:
        """Add a single platform tag to the layout.

        Args:
            text: Platform name
            is_empty: Whether this is an empty state tag
        """
        label = QLabel(text)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Style the tag
        if is_empty:
            # Gray style for "Nenhuma"
            label.setStyleSheet("""
                QLabel {
                    background-color: #F5F5F5;
                    color: #999999;
                    border: 1px solid #E0E0E0;
                    border-radius: 4px;
                    padding: 4px 8px;
                    font-size: 12px;
                    font-style: italic;
                }
            """)
        else:
            # Normal style for platform names
            label.setStyleSheet("""
                QLabel {
                    background-color: #E8E8E8;
                    color: #555555;
                    border: 1px solid #D0D0D0;
                    border-radius: 4px;
                    padding: 4px 8px;
                    font-size: 12px;
                    font-weight: 500;
                }
            """)

        self._flow_layout.addWidget(label)
