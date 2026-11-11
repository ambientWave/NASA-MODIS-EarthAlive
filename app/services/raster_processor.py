import rasterio
import numpy as np
import matplotlib.pyplot as plt

# change this into a dynamic code that can take the path of the file as an argument
# or create it if it's not there 
LST_PATH = "data/processed/lst_day_2024_01_01.tif"
QC_PATH = "data/processed/qc_day_2024_01_01.tif"


def analyze_lst_quality():

    with rasterio.open(LST_PATH) as lst_src:
        lst = lst_src.read(1)

    with rasterio.open(QC_PATH) as qc_src:
        qc = qc_src.read(1)

    # استبعاد القيم غير الصالحة من درجة الحرارة
    valid_lst = (lst >= 7500) & (lst <= 65535)

    # استخراج أول Bitين من بيانات الجودة
    mandatory_qa = qc & 0b00000011

    # اختيار القياسات ذات الجودة الجيدة
    good_quality = mandatory_qa == 0

    # الجمع بين صلاحية درجة الحرارة وجودة القياس
    valid_pixels = valid_lst & good_quality

    # تحويل Kelvin إلى Celsius
    temperature_celsius = lst[valid_pixels] * 0.02 - 273.15

    print("\n========== LST QUALITY ANALYSIS ==========")

    print("Total pixels:", lst.size)
    print("Valid LST pixels:", np.count_nonzero(valid_lst))
    print("Good quality pixels:", np.count_nonzero(valid_pixels))

    if temperature_celsius.size > 0:
        print("Mean temperature:", temperature_celsius.mean())
        print("Minimum temperature:", temperature_celsius.min())
        print("Maximum temperature:", temperature_celsius.max())
    else:
        print("No valid temperature pixels found.")

    print("==========================================")


def plot_temperature():

    with rasterio.open(LST_PATH) as lst_src:
        lst = lst_src.read(1)

    with rasterio.open(QC_PATH) as qc_src:
        qc = qc_src.read(1)

    valid_lst = (lst >= 7500) & (lst <= 65535)
    good_quality = (qc & 0b00000011) == 0

    valid_pixels = valid_lst & good_quality

    temperature = np.where(
        valid_pixels,
        lst * 0.02 - 273.15,
        np.nan
    )

    plt.figure(figsize=(10, 8))

    plt.imshow(temperature, cmap="inferno")
    plt.colorbar(label="Temperature (°C)")

    plt.title("MODIS Land Surface Temperature - 2024-01-01")
    plt.xlabel("Pixel Column")
    plt.ylabel("Pixel Row")

    plt.savefig(
        "data/processed/lst_heatmap.png",
        dpi=150,
        bbox_inches="tight"
    )

    plt.show()

    print("Heatmap saved successfully!")



if __name__ == "__main__":
    analyze_lst_quality()
    plot_temperature()    