from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base

# MYSQL CONNECTION STRING
# Format: mysql+pymysql://username:password@server_ip/database_name
# Example: If your password is root, it would be root:root@127.0.0.1
SQLALCHEMY_DATABASE_URL = "mysql+pymysql://root:12345@127.0.0.1:3306/bookletlog"

engine = create_engine(SQLALCHEMY_DATABASE_URL)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()