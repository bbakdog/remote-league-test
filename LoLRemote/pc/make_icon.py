"""exe 아이콘(icon.ico) 생성. build_exe.bat이 자동으로 실행합니다."""
from PIL import Image, ImageDraw

img = Image.new("RGBA", (256, 256), (10, 20, 40, 255))
d = ImageDraw.Draw(img)
d.ellipse((32, 32, 224, 224), outline=(200, 170, 110, 255), width=24)
d.ellipse((104, 104, 152, 152), fill=(200, 170, 110, 255))
img.save("icon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("icon.ico created")
