import tensorflow as tf
from tensorflow.keras.applications import MobileNetV2
from tensorflow.keras import layers, models

train_ds = tf.keras.utils.image_dataset_from_directory(
    "dataset", image_size=(224,224), batch_size=16,
    validation_split=0.2, subset="training", seed=42
)
val_ds = tf.keras.utils.image_dataset_from_directory(
    "dataset", image_size=(224,224), batch_size=16,
    validation_split=0.2, subset="validation", seed=42
)
print("CLASSES:", train_ds.class_names)   # confirm order — this is your label index mapping

base = MobileNetV2(input_shape=(224,224,3), include_top=False, weights='imagenet')
base.trainable = False

model = models.Sequential([
    layers.Rescaling(1./127.5, offset=-1),   # normalization baked in, no inference-side guessing
    base,
    layers.GlobalAveragePooling2D(),
    layers.Dense(32, activation='relu'),
    layers.Dropout(0.3),
    layers.Dense(3, activation='softmax')     # <-- the actual fix: 3 outputs, not 1
])

model.compile(
    optimizer='adam',
    loss='sparse_categorical_crossentropy',   # matches the integer labels image_dataset_from_directory gives you
    metrics=['accuracy']
)

model.fit(train_ds, validation_data=val_ds, epochs=10)

import numpy as np
test_img = np.random.randint(0,255,(1,224,224,3)).astype(np.float32)
pred = model.predict(test_img)
print("Sanity check — three values that should NOT all look like [0,0,1] every time:", pred)

converter = tf.lite.TFLiteConverter.from_keras_model(model)
with open("weather_model_v2.tflite", "wb") as f:
    f.write(converter.convert())
with open("labels.txt", "w") as f:
    for name in train_ds.class_names:
        f.write(name + "\n")