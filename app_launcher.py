"""Frozen entry point: dispatch worker processes before importing the GUI."""
import multiprocessing


if __name__ == '__main__':
    multiprocessing.freeze_support()
    from src.main import main
    main()
