#!/usr/bin/env python3

import os
import os.path
import struct
import sys
import hashlib

import difi
import savefilesystem
import key_engine

try:
    import secrets
except:
    class Secrets(object):
        pass


def getDigestBlock(saveType, saveId, header):
    if saveType == "nand":
        return b"CTR-SYS0" + struct.pack("<Q", saveId) + header
    sav0Block = hashlib.sha256(b"CTR-SAV0" + header).digest()
    return b"CTR-SIGN" + struct.pack("<Q", saveId) + sav0Block


def main():
    if len(sys.argv) < 2:
        print("Usage: %s input [output] [OPTIONS]" % sys.argv[0])
        print("")
        print("Arguments:")
        print("  input            A DISA file")
        print("  output           The directory for storing extracted files")
        print("")
        print("The following arguments are optional and are only needed for CMAC verification.")
        print("You need to provide secrets.py to enable CMAC verification.")
        print(
            "  -sd              Specify that the DISA file is a save file stored on SD card")
        print("  -nand            Specify that the DISA file is a save file stored on NAND")
        print("  -id ID           The save ID of the file in hex")
        exit(1)

    inputPath = None
    outputPath = None
    saveId = None
    saveType = None

    i = 1
    while i < len(sys.argv):
        if sys.argv[i] == "-id":
            i += 1
            saveId = int(sys.argv[i], 16)
        elif sys.argv[i] == "-sd":
            saveType = "sd"
        elif sys.argv[i] == "-nand":
            saveType = "nand"
        elif sys.argv[i] == "-card":
            saveType = "card"
        else:
            if inputPath is None:
                inputPath = sys.argv[i]
            else:
                outputPath = sys.argv[i]
        i += 1

    if inputPath is None:
        print("Error: no input file given.")
        exit(1)

    disa = open(inputPath, 'rb')

    secretsDb = secrets.Secrets()
    keyEngine = key_engine.KeyEngine(secretsDb)

    Cmac = disa.read(0x10)
    disa.seek(0x100, os.SEEK_SET)
    header = disa.read(0x100)

    if outputPath is None:
        print("No output directory given. Will only do data checking.")

    if saveType is None:
        print("No save type specified. Will skip CMAC verification.")
    elif saveType == "nand" or saveType == "sd":
        if saveId is None:
            print("No save ID specified. Will skip CMAC verification.")
        else:
            key = keyEngine.getKeySdNandCmac()
            if key is None:
                print("No enough secrets provided. Will skip CMAC verification.")
            else:
                digest = hashlib.sha256(getDigestBlock(
                    saveType, saveId, header)).digest()
                import cmac
                if Cmac != cmac.AesCmac(digest, key):
                    print("Error: CMAC mismatch.")
                    exit(1)
                else:
                    print("Info: CMAC verified.")
    else:
        print("Unsupported save type. Will skip CMAC verification.")

    # Reads DISA header
    disa.seek(0x100, os.SEEK_SET)
    DISA, ver, \
        partCount, secPartTableOff, priPartTableOff, partTableSize, \
        savePartEntryOff, savePartEntrySize, \
        dataPartEntryOff, dataPartEntrySize, \
        savePartOff, savePartSize, \
        dataPartOff, dataPartSize, \
        activeTable, unk1, unk2, tableHash = struct.unpack(
            '<IIQQQQQQQQQQQQBBH32s116x', header)

    if DISA != 0x41534944:
        print("Error: Not a DISA format")
        exit(1)

    if ver != 0x00040000:
        print("Error: Wrong DISA version")
        exit(1)

    if partCount == 1:
        hasData = False
        print("Info: No DATA partition")
    elif partCount == 2:
        hasData = True
        print("Info: Has DATA partition")
    else:
        print("Error: Wrong partition count %d" % parCount)
        exit(1)

    if activeTable == 0:
        partTableOff = priPartTableOff
    elif activeTable == 1:
        partTableOff = secPartTableOff
    else:
        print("Error: Wrong active table ID %d" % activeTable)
        exit(1)

    Unknown = unk1 + unk2 * 256
    if Unknown != 0:
        print("Warning: Unknown = 0x%X" % Unknown)

    # Verify partition table hash
    disa.seek(partTableOff, os.SEEK_SET)
    partTable = disa.read(partTableSize)

    if hashlib.sha256(partTable).digest() != tableHash:
        print("Error: Partition table hash mismatch!")
        exit(1)

    # Reads and unwraps SAVE image
    saveEntry = partTable[savePartEntryOff:
                          savePartEntryOff + savePartEntrySize]
    disa.seek(savePartOff, os.SEEK_SET)
    savePart = disa.read(savePartSize)
    saveImage, saveImageIsData = difi.unwrap(saveEntry, savePart)
    if saveImageIsData:
        print("Warning: SAVE partition is marked as DATA")

    # Reads and unwraps DATA image
    if hasData:
        dataEntry = partTable[dataPartEntryOff:
                              dataPartEntryOff + dataPartEntrySize]
        disa.seek(dataPartOff, os.SEEK_SET)
        dataPart = disa.read(dataPartSize)
        dataRegion, dataRegionIsData = difi.unwrap(dataEntry, dataPart)
        if not dataRegionIsData:
            print("Warning: DATA partition is not marked as DATA")

    disa.close()

    # Reads SAVE header
    SAVE, ver, filesystemHeaderOff, imageSize, imageBlockSize, x00 \
        = struct.unpack('<IIQQII', saveImage[0:0x20])

    if SAVE != 0x45564153:
        print("Error: Wrong SAVE magic")
        exit(1)

    if ver != 0x00040000:
        print("Error: Wrong SAVE version")
        exit(1)

    if x00 != 0:
        print("Warning: unknown 0 = 0x%X in SAVE header" % x00)

    fsHeader = savefilesystem.Header(
        saveImage[filesystemHeaderOff:filesystemHeaderOff + 0x68], hasData)

    if not hasData:
        dataRegion = saveImage[
            fsHeader.dataRegionOff: fsHeader.dataRegionOff +
            fsHeader.dataRegionSize * fsHeader.blockSize]

    # Parses hash tables
    dirHashTable = savefilesystem.getHashTable(fsHeader.dirHashTableOff,
                                               fsHeader.dirHashTableSize,
                                               saveImage)

    fileHashTable = savefilesystem.getHashTable(fsHeader.fileHashTableOff,
                                                fsHeader.fileHashTableSize,
                                                saveImage)

    # Parses FAT
    fat = savefilesystem.FAT(fsHeader, saveImage)

    # Parses directory & file entry table
    dirList = savefilesystem.getDirList(
        fsHeader, saveImage, dataRegion, fat)

    print("Directory list:")
    for i in range(len(dirList)):
        dirList[i].printEntry(i)

    fileList = savefilesystem.getFileList(
        fsHeader, saveImage, dataRegion, fat)

    print("File list:")
    for i in range(len(fileList)):
        fileList[i].printEntryAsSave(i)

    # Verifies directory & file hash table
    print("Verifying directory hash table")
    savefilesystem.verifyHashTable(dirHashTable, dirList)
    print("Verifying file hash table")
    savefilesystem.verifyHashTable(fileHashTable, fileList)

    # Walks through free blocks
    print("Walking through free blocks")
    fat.visitFreeBlock()

    def saveFileDumper(fileEntry, file, _):
        fileSize = fileEntry.size

        def blockDumper(index):
            nonlocal fileSize
            if fileSize == 0:
                print("Warning: excessive block")
                return
            tranSize = min(fileSize, fsHeader.blockSize)
            pos = index * fsHeader.blockSize
            if file is not None:
                file.write(dataRegion[pos: pos + tranSize])
            fileSize -= tranSize

        if fileSize != 0:
            fat.walk(fileEntry.blockIndex, blockDumper)
        if fileSize != 0:
            print("Warning: not enough block")

    print("Walking through files and dumping")
    savefilesystem.extractAll(dirList, fileList, outputPath, saveFileDumper)

    fat.allVisited()

    print("Finished!")


if __name__ == "__main__":
    main()
