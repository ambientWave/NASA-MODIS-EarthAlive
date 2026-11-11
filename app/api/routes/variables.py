from fastapi import APIRouter, HTTPException

router = APIRouter(
    prefix="/variables",
    tags=["Environmental Variables"]
)


VARIABLES = {

    "land_surface_temperature": {
        "name": "Land Surface Temperature",
        "description": "Temperature of the Earth's surface.",
        "unit": "Celsius",
        "satellite": "NASA MODIS",
        "dataset": "MOD11A1"
    },

    "ndvi": {
        "name": "Vegetation Index",
        "description": "Measures vegetation greenness and density.",
        "unit": "NDVI",
        "satellite": "NASA MODIS",
        "dataset": "MOD13Q1"
    },

    "precipitation": {
        "name": "Precipitation",
        "description": "Measures rainfall over a specific region.",
        "unit": "mm",
        "satellite": "NASA GPM",
        "dataset": "IMERG"
    },

    "soil_moisture": {
        "name": "Soil Moisture",
        "description": "Measures water content in soil.",
        "unit": "Volumetric Water Content",
        "satellite": "NASA SMAP",
        "dataset": "SMAP"
    },

    "evapotranspiration": {
        "name": "Evapotranspiration",
        "description": "Water transferred from land to atmosphere.",
        "unit": "mm",
        "satellite": "NASA MODIS",
        "dataset": "MOD16"
    },

    "air_temperature": {
        "name": "Air Temperature",
        "description": "Temperature of the atmosphere.",
        "unit": "Kelvin",
        "satellite": "NASA MERRA-2",
        "dataset": "MERRA-2"
    }
}


@router.get("/")
def get_variables():

    return {
        "status": "success",
        "count": len(VARIABLES),
        "data": VARIABLES
    }


@router.get("/{variable_id}")
def get_variable(variable_id: str):

    variable = VARIABLES.get(variable_id)

    if variable is None:
        raise HTTPException(
            status_code=404,
            detail="Environmental variable not found"
        )

    return {
        "status": "success",
        "data": variable
    }