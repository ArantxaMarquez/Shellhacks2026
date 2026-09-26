import sys
print("Starting script...", flush=True)

try:
    import tensorflow as tf
    print("TensorFlow Version:", tf.__version__, flush=True)

    # Test TFLite API
    interpreter = tf.lite.Interpreter
    print("TensorFlow Lite successfully loaded!", flush=True)
except Exception as e:
    print("Error encountered:", e, flush=True)