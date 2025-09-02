# app/auth.py
from datetime import datetime, timedelta
from jose import jwt, JWTError
from fastapi import Depends, HTTPException, status, APIRouter
from sqlalchemy.orm import Session
from passlib.context import CryptContext
from pydantic import BaseModel
from .db import get_db
from .models import User

# --- Router ---
auth_router = APIRouter()

# --- JWT settings ---
JWT_SECRET = "5N-gQ_9i-VHRs46bLi480UHXdSi1IYNzSafBq45HlYM"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

# --- Password hashing ---
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# --- OAuth2 scheme (for token verification) ---
from fastapi.security import OAuth2PasswordBearer
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

# --- Pydantic schema ---
class LoginRequest(BaseModel):
    email: str
    password: str

# --- Utility functions ---
def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

def create_access_token(user_id: str, role, warehouse_id: str | None) -> str:
    if hasattr(role, "value"):
        role = role.value  # convert Enum to string
    payload = {
        "sub": user_id,
        "role": role,
        "warehouse_id": warehouse_id,
        "exp": datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=ALGORITHM)

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid JWT")
        user = db.query(User).filter(User.id == user_id).one()
        return user
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

# --- Login route ---
@auth_router.post("/login")
def login(request: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == request.email).first()
    if not user or not verify_password(request.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    token = create_access_token(user.id, user.role, user.warehouse_id)
    return {"access_token": token, "token_type": "bearer"}
