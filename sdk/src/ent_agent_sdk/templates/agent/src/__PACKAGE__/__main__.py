from __PACKAGE__.agent import create_app


def main() -> None:
    create_app().run()  # port 8080, or PORT for local runs


if __name__ == "__main__":
    main()
