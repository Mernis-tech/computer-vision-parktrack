from ultralytics import YOLO
import cv2
import time
from datetime import datetime
from pathlib import Path

def multi_camera_segmentation():
    # Настройки
    model_path = r"C:\My files\YOLO PD\best.pt"
    base_output_dir = r"C:\My files\YOLO PD\runs\camera_output"
    
    # Загружаем модель
    model = YOLO(model_path)
    print("Модель загружена")
    
    # Настройки для двух камер
    cameras = [
        {
            'name': 'camera1',
            'url': 'https://cameras.inetcom.ru/hls/camera12_4.m3u8',
            'roi': [1400, 550, 1920, 1000],
            'last_time': 0
        },
        {
            'name': 'camera2', 
            'url': 'https://cameras.inetcom.ru/hls/camera12_11.m3u8',  # замените на реальный URL второй камеры
            'roi': [200, 275, 700, 800],  # настройте ROI для второй камеры
            'last_time': 0
        }
    ]
    
    # Создаем папки для каждой камеры
    for camera in cameras:
        camera_name = camera['name']
        segmented_dir = Path(base_output_dir) / camera_name / "segmented"
        cropped_dir = Path(base_output_dir) / camera_name / "cropped"
        
        
        segmented_dir.mkdir(parents=True, exist_ok=True)
        cropped_dir.mkdir(parents=True, exist_ok=True)
    
    # Открываем потоки для всех камер
    for camera in cameras:
        cap = cv2.VideoCapture(camera['url'])
        camera['cap'] = cap
        if not cap.isOpened():
            print(f"⚠️ Ошибка подключения к камере {camera['name']}")
        else:
            print(f"✓ Камера {camera['name']} подключена")
    
    print("Начинаем захват с двух камер...")
    
    try:
        while True:
            for camera in cameras:
                if 'cap' not in camera:
                    continue
                    
                cap = camera['cap']
                ret, frame = cap.read()
                
                if not ret:
                    print(f"⚠️ Ошибка чтения кадра с камеры {camera['name']}")
                    continue
                
                current_time = time.time()
                
                # Обрабатываем каждые 20 секунд для каждой камеры
                if current_time - camera['last_time'] >= 150:
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    camera_name = camera['name']
                    
                    print(f"\n📷 Обработка камеры: {camera_name}")
                    
                   
                    
                    # 2. Обрезаем кадр по ROI
                    x1, y1, x2, y2 = camera['roi']
                    cropped_frame = frame[y1:y2, x1:x2]
                    
                    # 3. Сохраняем обрезанное фото
                    cropped_dir = Path(base_output_dir) / camera_name / "cropped"
                    cropped_filename = f"cropped_{timestamp}.jpg"
                    cropped_path = cropped_dir / cropped_filename
                    cv2.imwrite(str(cropped_path), cropped_frame)
                    print(f"✓ Обрезанное фото сохранено: {cropped_filename}")
                    print(f"📐 Размер обрезанного фото: {cropped_frame.shape[1]}x{cropped_frame.shape[0]}")
                    
                    # 4. Выполняем сегментацию
                    results = model.predict(
                        source=cropped_frame,
                        imgsz=640,
                        conf=0.3,
                        iou=0.5,
                        augment=True,
                        verbose=False,
                        save=False
                    )
                    
                    if len(results) > 0:
                        # Визуализируем результаты
                        annotated_cropped = results[0].plot(
                            line_width=2,
                            font_size=1.0,
                            labels=True,
                            conf=True
                        )
                        
                        # Сохраняем сегментированное изображение
                        segmented_dir = Path(base_output_dir) / camera_name / "segmented"
                        segmented_filename = f"segmented_{timestamp}.jpg"
                        segmented_path = segmented_dir / segmented_filename
                        cv2.imwrite(str(segmented_path), annotated_cropped)
                        print(f"✓ Сегментированное фото сохранено: {segmented_filename}")
                        
                        # Информация о детекциях
                        boxes = results[0].boxes
                        if boxes is not None and len(boxes) > 0:
                            print(f"🚗 Обнаружено автомобилей: {len(boxes)}")
                            for i, box in enumerate(boxes):
                                conf = box.conf[0].item()
                                print(f"   Авто {i+1}: уверенность {conf:.3f}")
                        else:
                            print("❌ Автомобили не обнаружены")
                    
                    camera['last_time'] = current_time
                    print("-" * 50)
            
            # Небольшая задержка для уменьшения нагрузки
            time.sleep(0.1)
            
            # Выход по нажатию 'q'
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break
                
    except KeyboardInterrupt:
        print("Остановка пользователем...")
    finally:
        # Закрываем все потоки
        for camera in cameras:
            if 'cap' in camera:
                camera['cap'].release()
        cv2.destroyAllWindows()

if __name__ == "__main__":
    multi_camera_segmentation()



