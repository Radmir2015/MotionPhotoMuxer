import argparse
import logging
import os
import sys
import time
from os.path import exists, basename, isdir

import pyexiv2
import piexif
import pyheif
from PIL import Image

def heif_to_jpeg(heif_file):
    heif_image = pyheif.read(heif_file)
    image = Image.frombytes(
        heif_image.mode, 
        heif_image.size, 
        heif_image.data,
        "raw",
        heif_image.mode,
        heif_image.stride,
        )

    # Retrive the metadata
    for metadata in heif_image.metadata or []:
        if metadata['type'] == 'Exif':
            exif_dict = piexif.load(metadata['data'])

    # PIL rotates the image according to exif info, so it's necessary to remove the orientation tag otherwise the image will be rotated again (1° time from PIL, 2° from viewer).
    exif_dict['0th'][274] = 0
    exif_bytes = piexif.dump(exif_dict)
    return image, exif_bytes

def validate_directory(dir):
    
    if not exists(dir):
        logging.error("Path doesn't exist: {}".format(dir))
        exit(1)
    if not isdir(dir):
        logging.error("Path is not a directory: {}".format(dir))
        exit(1)

def validate_media(photo_path, video_path):
    """
    Checks if the files provided are valid inputs. Currently the only supported inputs are MP4/MOV and JPEG filetypes.
    Currently it only checks file extensions instead of actually checking file formats via file signature bytes.
    :param photo_path: path to the photo file
    :param video_path: path to the video file
    :return: True if photo and video files are valid, else False
    """
    if not exists(photo_path):
        logging.error("Photo does not exist: {}".format(photo_path))
        return False
    if not exists(video_path):
        logging.error("Video does not exist: {}".format(video_path))
        return False
    if not photo_path.lower().endswith(('.jpg', '.jpeg')) and not photo_path.lower().endswith(('.heic')):
        logging.error("Photo isn't a JPEG nor HEIC: {}".format(photo_path))
        return False
    if not video_path.lower().endswith(('.mov', '.mp4')):
        logging.error("Video isn't a MOV or MP4: {}".format(photo_path))
        return False
    return True

def merge_files(photo_path, video_path, output_path):
    """Merges the photo and video file together by concatenating the video at the end of the photo. Writes the output to
    a temporary folder.
    :param photo_path: Path to the photo
    :param video_path: Path to the video
    :return: File name of the merged output file
    """
    logging.info("Merging {} and {}.".format(photo_path, video_path))
    out_path = os.path.join(output_path, "{}".format(basename(photo_path)))
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "wb") as outfile, open(photo_path, "rb") as photo, open(video_path, "rb") as video:
        outfile.write(photo.read())
        outfile.write(video.read())
    logging.info("Merged photo and video.")
    return out_path


def add_xmp_metadata(merged_file, offset):
    """Adds XMP metadata to the merged image indicating the byte offset in the file where the video begins.
    :param merged_file: The path to the file that has the photo and video merged together.
    :param offset: The number of bytes from EOF to the beginning of the video.
    :return: None
    """
    metadata = pyexiv2.ImageMetadata(merged_file)
    logging.info("Reading existing metadata from file.")
    metadata.read()
    logging.info("Found XMP keys: " + str(metadata.xmp_keys))
    if len(metadata.xmp_keys) > 0:
        logging.warning("Found existing XMP keys. They *may* be affected after this process.")

    # (py)exiv2 raises an exception here on basically all my 'test' iPhone 13 photos -- I'm not sure why,
    # but it seems safe to ignore so far. It's logged anyways just in case.
    try:
        pyexiv2.xmp.register_namespace('http://ns.google.com/photos/1.0/camera/', 'GCamera')
    except KeyError:
        logging.warning("exiv2 detected that the GCamera namespace already exists.".format(merged_file))
    metadata['Xmp.GCamera.MicroVideo'] = pyexiv2.XmpTag('Xmp.GCamera.MicroVideo', 1)
    metadata['Xmp.GCamera.MicroVideoVersion'] = pyexiv2.XmpTag('Xmp.GCamera.MicroVideoVersion', 1)
    metadata['Xmp.GCamera.MicroVideoOffset'] = pyexiv2.XmpTag('Xmp.GCamera.MicroVideoOffset', offset)
    metadata['Xmp.GCamera.MicroVideoPresentationTimestampUs'] = pyexiv2.XmpTag(
        'Xmp.GCamera.MicroVideoPresentationTimestampUs',
        1500000)  # in Apple Live Photos, the chosen photo is 1.5s after the start of the video, so 1500000 microseconds
    metadata.write()


def convert(photo_path, video_path, output_path):
    """
    Performs the conversion process to mux the files together into a Google Motion Photo.
    :param photo_path: path to the photo to merge
    :param video_path: path to the video to merge
    :return: True if conversion was successful, else False
    """
    if photo_path.lower().endswith('.heic'):
        image, exif = heif_to_jpeg(photo_path)
        photo_path = photo_path.replace('.heic', '.jpg').replace('.HEIC', '.jpg')
        image.save(photo_path, exif=exif, quality=95, subsampling=0)
    merged = merge_files(photo_path, video_path, output_path)
    photo_filesize = os.path.getsize(photo_path)
    merged_filesize = os.path.getsize(merged)

    # The 'offset' field in the XMP metadata should be the offset (in bytes) from the end of the file to the part
    # where the video portion of the merged file begins. In other words, merged size - photo_only_size = offset.
    offset = merged_filesize - photo_filesize
    add_xmp_metadata(merged, offset)

    # Deleting the temporary .jpg file if the original was in HEIC format
    if photo_path.lower().endswith('.jpg') and (os.path.exists(photo_path.replace('.jpg', '.HEIC') or os.path.exists(photo_path.replace('.jpg', '.heic')))):
        os.remove(photo_path)

def matching_video(photo_path):
    base = os.path.splitext(photo_path)[0]
    logging.info("Looking for videos named: {}".format(base))
    if os.path.exists(base + ".mov"):
        return base + ".mov"
    if os.path.exists(base + ".mp4"):
        return base + ".mp4"
    if os.path.exists(base + ".MOV"):
        return base + ".MOV"
    if os.path.exists(base + ".MP4"):
        return base + ".MP4"
    else:
        return ""


def process_directory(file_dir, recurse):
    """
    Loops through files in the specified directory and generates a list of (photo, video) path tuples that can
    be converted
    :TODO: Implement recursive scan
    :param file_dir: directory to look for photos/videos to convert
    :param recurse: if true, subdirectories will recursively be processes
    :return: a list of tuples containing matched photo/video pairs.
    """
    logging.info("Processing dir: {}".format(file_dir))
    if recurse:
        logging.error("Recursive traversal is not implemented yet.")
        exit(1)

    file_pairs = []
    for file in os.listdir(file_dir):
        file_fullpath = os.path.join(file_dir, file)
        if os.path.isfile(file_fullpath) and (file.lower().endswith(('.jpg', '.jpeg')) or file.lower().endswith('.heic')) and matching_video(
                file_fullpath) != "":
            file_pairs.append((file_fullpath, matching_video(file_fullpath)))

    logging.info("Found {} pairs.".format(len(file_pairs)))
    logging.info("subset of found image/video pairs: {}".format(str(file_pairs[0:9])))
    return file_pairs

def print_status_bar(filename, current, total, bar_length=50, elapsed_time=None):
    """
    Prints a status bar in the console.
    :param filename: Current processing file.
    :param current: Current file number.
    :param total: Total number of files.
    :param bar_length: The length of the progress bar in characters.
    :param elapsed_time: Time taken to process current file.
    """
    progress = float(current) / total
    arrow = '=' * int(round(progress * bar_length) - 1) + '>'
    spaces = ' ' * (bar_length - len(arrow))

    if elapsed_time and current > 0:
        estimated_remaining = (elapsed_time / current) * (total - current)
        time_msg = 'ETA: {} sec '.format(time.strftime('%H:%M:%S', time.gmtime(estimated_remaining)))
    else:
        time_msg = ''

    # print(filename, current, total, bar_length, elapsed_time)
    os.system('cls||clear')
    sys.stdout.write('\r[{0}] {1}%\nFile: {2} ({3}/{4})\n{5}'.format(arrow + spaces, int(progress * 100), filename, current, total, time_msg))
    # sys.stdout.flush()

def main(args):
    logging_level = logging.INFO if args.verbose else logging.ERROR
    logging.basicConfig(level=logging_level, stream=sys.stdout)

    outdir = args.output if args.output is not None else "output"

    error_files = []
    total_processed = 0
    successful = 0

    start_time = time.time()

    if args.dir is not None:
        validate_directory(args.dir)
        pairs = process_directory(args.dir, args.recurse)
        total_files = len(pairs)
        for index, pair in enumerate(pairs):
            file_start_time = time.time()
            if validate_media(pair[0], pair[1]):
                try:
                    convert(pair[0], pair[1], outdir)
                    successful += 1
                except Exception as e:
                    error_files.append(pair[0])
                    logging.error(f"Error processing {pair[0]}: {str(e)}")
                total_processed += 1
                elapsed_time = time.time() - start_time
                print_status_bar(basename(pair[0]), index + 1, total_files, elapsed_time=elapsed_time)
        # print("\n")
    else:
        if args.photo is None and args.video is None:
            logging.error("Either --dir or --photo and --video are required.")
            exit(1)

        if bool(args.photo) ^ bool(args.video):
            logging.error("Both --photo and --video must be provided.")
            exit(1)

        total_files = 1
        if validate_media(args.photo, args.video):
            file_start_time = time.time()
            try:
                convert(args.photo, args.video, outdir)
                successful += 1
            except Exception as e:
                error_files.append(args.photo)
                logging.error(f"Error processing {args.photo}: {str(e)}")
            total_processed += 1
            elapsed_time = time.time() - file_start_time
            print_status_bar(basename(args.photo), 1, total_files, elapsed_time=elapsed_time)
        # print("\n")

    end_time = time.time()

    print("\nElapsed time (real): {} sec".format(time.strftime('%H:%M:%S', time.gmtime(end_time - start_time))))
    print(f"Finished processing {total_processed} files. {successful} processed successfully, {len(error_files)} with errors.")
    if error_files:
        print("Files with errors:")
        for ef in error_files:
            print(ef)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Merges a photo and video into a Microvideo-formatted Google Motion Photo')
    parser.add_argument('--verbose', help='Show logging messages.', action='store_true')
    parser.add_argument('--dir', type=str, help='Process a directory for photos/videos. Takes precedence over '
                                                '--photo/--video')
    parser.add_argument('--recurse', help='Recursively process a directory. Only applies if --dir is also provided',
                        action='store_true')
    parser.add_argument('--photo', type=str, help='Path to the JPEG photo to add.')
    parser.add_argument('--video', type=str, help='Path to the MOV video to add.')
    parser.add_argument('--output', type=str, help='Path to where files should be written out to.')
    main(parser.parse_args())

