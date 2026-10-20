from models import engine, initialize_database, seed_demo_data


if __name__ == "__main__":
    initialize_database(engine)
    seeded = seed_demo_data(force=True)
    if seeded:
        print("Smart Mini-Dairy database initialized with demo data in data.db")
    else:
        print("Smart Mini-Dairy database initialized in data.db")
