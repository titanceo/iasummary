import tkinter as tk

from app.gui import ConverterUI


def main() -> None:
    root = tk.Tk()
    ConverterUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
