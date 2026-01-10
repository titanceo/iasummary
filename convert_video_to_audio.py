import os

# Allow OpenMP duplicate runtime on Windows to avoid libiomp5md.dll init error.
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

from app.main import main


if __name__ == "__main__":
    main()
