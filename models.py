from sqlalchemy import Column, Integer, String, Float, Date, ForeignKey
from sqlalchemy.orm import relationship, DeclarativeBase


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    discord_id = Column(String, unique=True, index=True)
    username = Column(String)
    avatar = Column(String, nullable=True)


class PnLEntry(Base):
    __tablename__ = "pnl_entries"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    date = Column(Date)
    wins = Column(Integer, default=0)
    losses = Column(Integer, default=0)
    profit = Column(Float, default=0.0)

    user = relationship("User")


class Earnings(Base):
    __tablename__ = "earnings"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String)
    report_date = Column(Date)
    fiscal_year = Column(Integer, nullable=True)
    fiscal_period = Column(String, nullable=True)