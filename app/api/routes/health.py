from fastapi import APIRouter

router = APIRouter(
    prefix="/health",
    tags=["Health"]
)


@router.get("/")
def health_check():

    return {
        "status": "success",
        "message": "Earth Alive Backend is running!",
        "service": "Earth Alive API",
        "version": "1.0.0"
    }