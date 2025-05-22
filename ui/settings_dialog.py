# ui/settings_dialog.py
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QPushButton, QCheckBox, QComboBox,
    QMessageBox, QWidget, QFileDialog, QSpinBox, QGroupBox # Added QGroupBox
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut, QFontMetrics, QFont
import pathlib
from typing import Dict, Any, List, Optional

from utils.config import (
    CONFIG_ITEM_DEFINITIONS, ConfigItem, ConfigUIType, ConfigChoice,
    USER_CONFIG, save_user_config, ConfigCategory
)
from .components import get_font, FONT_SIZE_NORMAL

class SettingsDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Application Settings")
        self.setMinimumWidth(550) # Adjusted width
        self.setModal(True)

        self._ui_elements: Dict[str, QWidget] = {} # To store QLineEdit, QCheckBox, QComboBox
        self._path_buttons: Dict[str, QPushButton] = {} # For browse buttons

        self._setup_ui()
        self._load_settings()

        # Shortcut for Escape key to close/reject the dialog
        escape_shortcut = QShortcut(QKeySequence(Qt.Key_Escape), self)
        escape_shortcut.activated.connect(self.reject)
        
        # Add Ctrl+W shortcut to close
        close_shortcut = QShortcut(QKeySequence("Ctrl+W"), self)
        close_shortcut.activated.connect(self.reject)


    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(15, 15, 15, 15)
        main_layout.setSpacing(10) # Spacing between sections and bottom buttons

        # Calculate max label width for items using custom row layout (CHOICE, NUMBER) to align their inputs
        # This should be done globally if alignment across sections is desired for these types.
        max_custom_row_label_width = 0
        label_font_for_calc = get_font(FONT_SIZE_NORMAL)
        fm = QFontMetrics(label_font_for_calc)
        for config_item_def in CONFIG_ITEM_DEFINITIONS:
            if config_item_def.show_in_settings_ui and \
               (config_item_def.ui_type == ConfigUIType.CHOICE or config_item_def.ui_type == ConfigUIType.NUMBER):
                text_width = fm.horizontalAdvance(f"{config_item_def.label}:")
                if text_width > max_custom_row_label_width:
                    max_custom_row_label_width = text_width
        
        if max_custom_row_label_width > 0:
            max_custom_row_label_width += fm.horizontalAdvance("  ") # Add padding

        # Define categories and their order
        ordered_categories = [ConfigCategory.BEHAVIOR, ConfigCategory.APPEARANCE]

        for category in ordered_categories:
            # Create a QGroupBox for the section
            section_group_box = QGroupBox(category.value) # Use category name as title
            group_box_font = get_font(FONT_SIZE_NORMAL, weight=QFont.Weight.Bold) # Make title bold
            section_group_box.setFont(group_box_font)
            
            # Layout for the content of the QGroupBox (this will be the QFormLayout)
            group_box_content_layout = QVBoxLayout(section_group_box) # Use QVBoxLayout to hold the form
            group_box_content_layout.setContentsMargins(10,10,10,10) # Inner margins for group box content
            group_box_content_layout.setSpacing(10)


            # Create a QFormLayout for this section's settings (will go inside the group box)
            section_form_layout = QFormLayout() # Don't parent it to a widget yet
            section_form_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
            section_form_layout.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
            section_form_layout.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
            section_form_layout.setHorizontalSpacing(10)
            section_form_layout.setVerticalSpacing(10)

            items_in_category = [item for item in CONFIG_ITEM_DEFINITIONS if item.category == category and item.show_in_settings_ui]

            if not items_in_category:
                no_items_label = QLabel("No settings available in this category.")
                no_items_label.setFont(get_font(FONT_SIZE_NORMAL, italic=True))
                no_items_label.setStyleSheet("color: grey;")
                # For group box, it's better to add this to its layout directly
                # If using QFormLayout inside, it will look a bit off without a field.
                # Let's add it to the group_box_content_layout if form layout is empty.
                temp_widget_for_no_items = QWidget()
                temp_layout_for_no_items = QVBoxLayout(temp_widget_for_no_items)
                temp_layout_for_no_items.addWidget(no_items_label)
                temp_layout_for_no_items.setContentsMargins(0,0,0,0)
                group_box_content_layout.addWidget(temp_widget_for_no_items)
                main_layout.addWidget(section_group_box)
                # main_layout.addSpacing(15) # Spacing is handled by main_layout.setSpacing()
                continue

            # Add items to the section_form_layout
            for config_item in items_in_category:
                label_text = f"{config_item.label}:"
                label_widget = QLabel(label_text)
                label_widget.setFont(get_font(FONT_SIZE_NORMAL))
                if config_item.tooltip:
                    label_widget.setToolTip(config_item.tooltip)

                field_widget: Optional[QWidget] = None
                field_container_widget: Optional[QWidget] = None

                if config_item.ui_type == ConfigUIType.BOOL:
                    field_widget = QCheckBox(config_item.label)
                    field_widget.setFont(get_font(FONT_SIZE_NORMAL))
                    if config_item.tooltip:
                        field_widget.setToolTip(config_item.tooltip)
                    self._ui_elements[config_item.key] = field_widget
                    section_form_layout.addRow(field_widget)
                    continue
                elif config_item.ui_type == ConfigUIType.STRING:
                    field_widget = QLineEdit()
                    field_widget.setFont(get_font(FONT_SIZE_NORMAL))
                    if config_item.placeholder:
                        field_widget.setPlaceholderText(config_item.placeholder)
                    if config_item.tooltip:
                        field_widget.setToolTip(config_item.tooltip)
                elif config_item.ui_type == ConfigUIType.PATH_STRING:
                    path_layout = QHBoxLayout()
                    path_layout.setContentsMargins(0,0,0,0)
                    path_layout.setSpacing(5)
                    
                    path_edit = QLineEdit()
                    path_edit.setFont(get_font(FONT_SIZE_NORMAL))
                    if config_item.placeholder:
                        path_edit.setPlaceholderText(config_item.placeholder)
                    if config_item.tooltip:
                        path_edit.setToolTip(config_item.tooltip)
                    path_layout.addWidget(path_edit, 1)

                    browse_button = QPushButton("Browse...")
                    browse_button.setFont(get_font(FONT_SIZE_NORMAL))
                    browse_button.setToolTip(f"Browse for {config_item.label.lower()}")
                    path_layout.addWidget(browse_button)

                    self._path_buttons[config_item.key] = browse_button
                    field_widget = path_edit
                    field_container_widget = QWidget()
                    field_container_widget.setLayout(path_layout)

                elif config_item.ui_type == ConfigUIType.CHOICE:
                    actual_combo_box = QComboBox()
                    actual_combo_box.setFont(get_font(FONT_SIZE_NORMAL))
                    if config_item.choices:
                        for choice in config_item.choices:
                            actual_combo_box.addItem(choice.display, choice.value)
                    if config_item.tooltip:
                        actual_combo_box.setToolTip(config_item.tooltip)
                    
                    if max_custom_row_label_width > 0:
                        label_widget.setMinimumWidth(max_custom_row_label_width)
                    label_widget.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

                    custom_row_container_widget = QWidget()
                    custom_row_layout = QHBoxLayout(custom_row_container_widget)
                    custom_row_layout.setContentsMargins(0,0,0,0)
                    custom_row_layout.setSpacing(section_form_layout.horizontalSpacing())

                    custom_row_layout.addWidget(label_widget)
                    custom_row_layout.addWidget(actual_combo_box)
                    custom_row_layout.addStretch(1)

                    self._ui_elements[config_item.key] = actual_combo_box
                    section_form_layout.addRow(custom_row_container_widget)
                    continue
                
                elif config_item.ui_type == ConfigUIType.NUMBER:
                    actual_spin_box = QSpinBox()
                    actual_spin_box.setFont(get_font(FONT_SIZE_NORMAL))
                    if config_item.min_val is not None:
                        actual_spin_box.setMinimum(config_item.min_val)
                    if config_item.max_val is not None:
                        actual_spin_box.setMaximum(config_item.max_val)
                    else:
                        actual_spin_box.setMaximum(99999)
                    if config_item.tooltip:
                        actual_spin_box.setToolTip(config_item.tooltip)
                    
                    if max_custom_row_label_width > 0:
                        label_widget.setMinimumWidth(max_custom_row_label_width)
                    label_widget.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

                    custom_row_container_widget = QWidget()
                    custom_row_layout = QHBoxLayout(custom_row_container_widget)
                    custom_row_layout.setContentsMargins(0,0,0,0)
                    custom_row_layout.setSpacing(section_form_layout.horizontalSpacing())

                    custom_row_layout.addWidget(label_widget)
                    custom_row_layout.addWidget(actual_spin_box)
                    custom_row_layout.addStretch(1)

                    self._ui_elements[config_item.key] = actual_spin_box
                    section_form_layout.addRow(custom_row_container_widget)
                    continue

                if field_widget:
                    self._ui_elements[config_item.key] = field_widget
                    section_form_layout.addRow(label_widget, field_container_widget if field_container_widget else field_widget)
                else:
                    print(f"Warning: UI element for config key '{config_item.key}' of type '{config_item.ui_type}' was not created.")
            
            # Add the form layout to the group box's content layout
            group_box_content_layout.addLayout(section_form_layout)
            main_layout.addWidget(section_group_box)
            # main_layout.addSpacing(15) # Spacing between group boxes is handled by main_layout.setSpacing()


        # Connect browse buttons (this can stay as it iterates all _path_buttons)
        for key, browse_btn in self._path_buttons.items():
            if key in self._ui_elements and isinstance(self._ui_elements[key], QLineEdit):
                config_item_for_path = next((item for item in CONFIG_ITEM_DEFINITIONS if item.key == key), None)
                dialog_title = f"Select {config_item_for_path.label}" if config_item_for_path else "Select Directory"
                browse_btn.clicked.connect(
                    lambda checked=False, k=key, title=dialog_title: self._browse_for_path(k, title)
                )
        
        # main_layout.addWidget(form_widget) # This is removed as form_widget is now per-section

        # Buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch(1)

        self.reset_button = QPushButton("Reset to Defaults")
        self.reset_button.setFont(get_font(FONT_SIZE_NORMAL))
        self.reset_button.setToolTip("Reset all settings on this page to their default values. Does not save them.")
        self.reset_button.clicked.connect(self._reset_to_defaults)
        button_layout.addWidget(self.reset_button)
        
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setFont(get_font(FONT_SIZE_NORMAL))
        self.cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(self.cancel_button)

        self.save_button = QPushButton("Save")
        self.save_button.setFont(get_font(FONT_SIZE_NORMAL))
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self._save_settings)
        button_layout.addWidget(self.save_button)

        main_layout.addLayout(button_layout)
        self.setLayout(main_layout)

    def _browse_for_path(self, config_key: str, dialog_title: str):
        line_edit = self._ui_elements.get(config_key)
        if not isinstance(line_edit, QLineEdit):
            return

        current_path_str = line_edit.text()
        # Try to make current_path an absolute path for the dialog's starting directory
        start_dir = str(pathlib.Path(current_path_str).expanduser().resolve() if current_path_str else pathlib.Path.home())

        directory = QFileDialog.getExistingDirectory(
            self,
            dialog_title,
            start_dir,
            QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontResolveSymlinks
        )
        if directory:
            line_edit.setText(directory)


    def _load_settings(self):
        for config_item in CONFIG_ITEM_DEFINITIONS:
            key = config_item.key
            current_value = USER_CONFIG.get(key, config_item.default)
            
            widget = self._ui_elements.get(key)
            if not widget:
                continue

            if config_item.ui_type == ConfigUIType.BOOL:
                if isinstance(widget, QCheckBox):
                    widget.setChecked(bool(current_value))
            elif config_item.ui_type == ConfigUIType.STRING or config_item.ui_type == ConfigUIType.PATH_STRING:
                if isinstance(widget, QLineEdit):
                    widget.setText(str(current_value) if current_value is not None else "")
            elif config_item.ui_type == ConfigUIType.CHOICE:
                if isinstance(widget, QComboBox):
                    found_idx = -1
                    for i in range(widget.count()):
                        if widget.itemData(i) == current_value:
                            found_idx = i
                            break
                    if found_idx != -1:
                        widget.setCurrentIndex(found_idx)
                    else:
                        default_choice_val = config_item.default
                        for i in range(widget.count()):
                            if widget.itemData(i) == default_choice_val:
                                widget.setCurrentIndex(i)
                                break
            elif config_item.ui_type == ConfigUIType.NUMBER:
                if isinstance(widget, QSpinBox):
                    widget.setValue(int(current_value))

    def _save_settings(self):
        something_changed = False
        theme_changed_specifically = False

        for config_item in CONFIG_ITEM_DEFINITIONS:
            key = config_item.key
            widget = self._ui_elements.get(key)
            if not widget:
                continue

            new_value: Any = None
            if config_item.ui_type == ConfigUIType.BOOL:
                if isinstance(widget, QCheckBox):
                    new_value = widget.isChecked()
            elif config_item.ui_type == ConfigUIType.STRING or config_item.ui_type == ConfigUIType.PATH_STRING:
                if isinstance(widget, QLineEdit):
                    new_value = widget.text()
                    if key == "default_filename" and not new_value.strip():
                        new_value = None
            elif config_item.ui_type == ConfigUIType.CHOICE:
                if isinstance(widget, QComboBox):
                    new_value = widget.currentData()
            elif config_item.ui_type == ConfigUIType.NUMBER:
                if isinstance(widget, QSpinBox):
                    new_value = widget.value()


            current_user_config_val = USER_CONFIG.get(key)
            # For numbers, ensure we are comparing compatible types if one is None or from JSON
            if config_item.ui_type == ConfigUIType.NUMBER:
                 # Convert to int for comparison, handling None from USER_CONFIG if key is new
                current_val_for_comp = int(current_user_config_val) if current_user_config_val is not None else None
                new_val_for_comp = int(new_value) # QSpinBox.value() returns int
                
                # Check if new_value is different from current or (if current doesn't exist) different from default
                if current_val_for_comp != new_val_for_comp or \
                   (key not in USER_CONFIG and new_val_for_comp != int(config_item.default)):
                    USER_CONFIG[key] = new_value # Store the original new_value (which is already int)
                    something_changed = True
                    # No specific handling for theme_changed_specifically for NUMBER types
            elif current_user_config_val != new_value or (key not in USER_CONFIG and new_value != config_item.default):
                USER_CONFIG[key] = new_value
                something_changed = True
                if key == "theme":
                    theme_changed_specifically = True
        
        if something_changed:
            if save_user_config():
                self.accept()
            else:
                QMessageBox.warning(self, "Save Error", "Could not save your settings to the configuration file.")
        else:
            self.accept()

    def _reset_to_defaults(self):
        reply = QMessageBox.question(self, "Reset Settings",
                                           "Are you sure you want to reset settings on this page to their default values?\n"
                                           "This will not save them until you click 'Save'.",
                                           QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                           QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            for config_item in CONFIG_ITEM_DEFINITIONS:
                key = config_item.key
                default_value = config_item.default
                
                widget = self._ui_elements.get(key)
                if not widget:
                    continue

                if config_item.ui_type == ConfigUIType.BOOL:
                    if isinstance(widget, QCheckBox): widget.setChecked(bool(default_value))
                elif config_item.ui_type == ConfigUIType.STRING or config_item.ui_type == ConfigUIType.PATH_STRING:
                    if isinstance(widget, QLineEdit): widget.setText(str(default_value) if default_value is not None else "")
                elif config_item.ui_type == ConfigUIType.CHOICE:
                    if isinstance(widget, QComboBox):
                        for i in range(widget.count()):
                            if widget.itemData(i) == default_value:
                                widget.setCurrentIndex(i)
                                break
                elif config_item.ui_type == ConfigUIType.NUMBER:
                    if isinstance(widget, QSpinBox): widget.setValue(int(default_value))

            QMessageBox.information(self, "Settings Reset", "Settings on this page have been reset to their defaults. Click 'Save' to apply these changes.")
