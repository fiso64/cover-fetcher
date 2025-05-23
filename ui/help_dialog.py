# ui/help_dialog.py
from PySide6.QtWidgets import QDialog, QVBoxLayout, QTextEdit, QDialogButtonBox, QScrollArea, QWidget, QGridLayout, QLabel
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontMetrics
from typing import List, Dict, Any, Tuple

class HelpDialog(QDialog):
    def __init__(self, arg_definitions: List[Tuple[List[str], Dict[str, Any]]], parent: QWidget = None):
        super().__init__(parent)
        self.setWindowTitle("Command Line Options - Cover Fetcher")
        self.setMinimumWidth(300)
        self.setMinimumHeight(300)
        self.resize(800, 610)

        main_layout = QVBoxLayout(self)

        scroll_area = QScrollArea(self)
        scroll_area.setWidgetResizable(True)
        main_layout.addWidget(scroll_area)

        content_widget = QWidget()
        scroll_area.setWidget(content_widget)
        
        grid_layout = QGridLayout(content_widget)
        grid_layout.setColumnStretch(0, 1) # Flags column
        grid_layout.setColumnStretch(1, 3) # Description column
        content_widget.setLayout(grid_layout)

        # Header
        header_flags = QLabel("<b>Argument</b>")
        header_desc = QLabel("<b>Description</b>")
        grid_layout.addWidget(header_flags, 0, 0)
        grid_layout.addWidget(header_desc, 0, 1)
        
        # Separator (optional, for visual distinction)
        line = QWidget()
        line.setFixedHeight(1)
        line.setStyleSheet("background-color: #c0c0c0;")
        grid_layout.addWidget(line, 1, 0, 1, 2)


        current_row = 2
        for flags_list, options_dict in arg_definitions:
            # flags_list = arg_def["flags_or_name"] # Old way
            # options = arg_def.get("options", {}) # Old way
            
            # New way: flags_list and options_dict are directly from the tuple
            options = options_dict # Use 'options' as the variable name for consistency with rest of the method

            flags_str_parts = []
            for flag_item in flags_list:
                if not flag_item.startswith("-"): # Positional
                    metavar = options.get("metavar") or flag_item.upper()
                    flags_str_parts.append(f"<{metavar}>")
                else:
                    flags_str_parts.append(flag_item)
            
            flags_display = ", ".join(flags_str_parts)
            
            # Add metavar to flags if it exists and is not for positional
            if options.get("metavar") and flags_list[0].startswith("-"):
                if not any(f"<{options['metavar']}>" in part for part in flags_str_parts):
                     flags_display += f" <{options['metavar']}>"


            flag_label = QLabel(f"<b>{flags_display}</b>")
            flag_label.setTextFormat(Qt.RichText)
            flag_label.setWordWrap(True)
            flag_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

            help_text = options.get("help", "No description available.")
            # You could add more info like type, default here if desired
            # e.g. if options.get('type'): help_text += f" (Type: {options['type'].__name__})"
            # e.g. if 'default' in options and options['default'] is not None and options['default'] != argparse.SUPPRESS:
            #    help_text += f" (Default: {options['default']})"
            
            desc_label = QLabel(help_text)
            desc_label.setWordWrap(True)
            desc_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            
            grid_layout.addWidget(flag_label, current_row, 0, Qt.AlignTop)
            grid_layout.addWidget(desc_label, current_row, 1, Qt.AlignTop)
            current_row += 1

        grid_layout.setRowStretch(current_row, 1) # Push content to top

        button_box = QDialogButtonBox(QDialogButtonBox.Ok)
        button_box.accepted.connect(self.accept)
        main_layout.addWidget(button_box)

        self.setLayout(main_layout)