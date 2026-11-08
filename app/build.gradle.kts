plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

android {
    namespace = "com.abyale.owletmonitor"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.abyale.owletmonitor"
        minSdk = 26
        targetSdk = 34
        versionCode = (System.getenv("GITHUB_RUN_NUMBER") ?: "1").toInt()
        versionName = "1.2." + (System.getenv("GITHUB_RUN_NUMBER") ?: "0")
        ndk {
            // arm64 = almost every phone from the last 7 years
            abiFilters += listOf("arm64-v8a", "armeabi-v7a")
        }
    }

    // One fixed signing key, so new builds install as updates (your settings and history are kept).
    signingConfigs {
        create("fixed") {
            storeFile = file("owlet-monitor.keystore")
            storePassword = "owletmonitor"
            keyAlias = "owlet"
            keyPassword = "owletmonitor"
        }
    }

    buildTypes {
        getByName("release") {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("fixed")
        }
        getByName("debug") {
            signingConfig = signingConfigs.getByName("fixed")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    lint {
        abortOnError = false
        checkReleaseBuilds = false
    }
}

chaquopy {
    defaultConfig {
        version = "3.11"
        pip {
            install("pyowletapi==2025.4.10")
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
}
